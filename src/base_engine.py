import os
import sys
import torch
import torch.distributed as dist
import numpy as np
import logging
logger = logging.getLogger("logger")
try:
    from apex import amp
    APEX_IMPORTED = True
except:
    logger.info("Install nvidia apex first")
    APEX_IMPORTED = False

class BaseEngine():
    def __init__(self, cfg, graph, loader, solvers, visualizer):
        self.cfg = cfg
        self.graph = graph       
        self.solvers = solvers
        self.tdata = loader['train'] if 'train' in loader else None
        self.vdata = loader['val'] if 'val' in loader else None
        self.qdata = loader['query'] if 'query' in loader else None
        self.gdata = loader['gallery'] if 'gallery' in loader else None
        self.visualizer = visualizer

        self.iter = 0
        self.epoch = 0
        self.max_epoch = cfg.SOLVER.MAX_EPOCHS
        self.use_gpu = graph.use_gpu
        self.loss = 1e5
        self.losses = None
        self.train_accu = 0.0
        self.best_accu = 0.0
        self.min_loss = 1e5
        self.test_loss = 0.0
        self.accu = 0.0
        self.save_criterion = cfg.MODEL.SAVE_CRITERION
        if self.cfg.DISTRIBUTED:
            self.device = torch.cuda.current_device()
        else:
            self.device = -1

    def _start(self):
        logger.info("Training start")
        self.iter = (self.cfg.SOLVER.START_EPOCH - 1) * len(self.tdata)
        self.epoch = self.cfg.SOLVER.START_EPOCH - 1

    def _train_epoch_start(self):
        self.epoch += 1
        logger.info(f"Epoch {self.epoch} start")
        self.graph.model.train() 
        if self.cfg.DISTRIBUTED:
            self.tdata.sampler.set_epoch(self.epoch)
        if len(self.graph.sub_models) > 0:
            for sub_model in self.graph.sub_models:
                self.graph.sub_models[sub_model].train()
  
    def _train_iter_start(self):
        self.iter += 1
        for solver in self.solvers:
            self.solvers[solver].lr_adjust(self.loss, self.iter)
            self.solvers[solver].zero_grad()

    def _train_iter_end(self): 
        if self.cfg.APEX and APEX_IMPORTED:
            with amp.scale_loss(self.loss, self.solvers['main'].opt) as scaled_loss:
                scaled_loss.backward()  
        else:       
            self.loss.backward() 
        for solver in self.solvers:
            self.solvers[solver].step()

        if self.cfg.DISTRIBUTED:
            dist.all_reduce(self.loss)
            self.loss.detach_()
            self.loss.div_(dist.get_world_size())
            for loss in self.losses:
                self.losses[loss].detach_()
                dist.all_reduce(self.losses[loss])
                self.losses[loss].div_(dist.get_world_size())

        self.loss = self.tensor_to_scalar(self.loss)
        self.losses = self.tensor_to_scalar(self.losses)     
        if self.cfg.IO:
            self.visualizer.add_scalar('train/loss', self.loss, self.iter)              
            for loss in self.losses:
                self.visualizer.add_scalar(f'train/loss/{loss}', self.losses[loss], self.iter)
            self.visualizer.add_scalar('train/accuracy', self.train_accu, self.iter)   
            for solver in self.solvers:
                self.visualizer.add_scalar(f'train/solver/{solver}/lr', self.solvers[solver].monitor_lr, self.iter)

    def _train_epoch_end(self):        
        logger.info(f"Epoch {self.epoch} training ends, accuracy {self.train_accu:.4f}")

    def _eval_epoch_start(self): 
        self.graph.model.eval() 

    def _eval_iter_start(self):
        raise NotImplementedError  

    def _eval_iter_end(self):           
        raise NotImplementedError

    def _eval_epoch_end(self):
        if self.cfg.IO:
            if self.save_criterion == 'loss':
                logger.info(f"Epoch {self.epoch} evaluation ends, loss {self.test_loss:.4f}")
                if self.min_loss > self.test_loss:
                    if self.cfg.SAVE:
                        logger.info(f"Save checkpoint, with {self.min_loss - self.test_loss:.4f} improvement")
                        self.graph.save(self.graph.save_path, self.graph.model, self.graph.sub_models, self.solvers, self.epoch, self.min_loss)
                    self.min_loss = self.test_loss
                self.visualizer.add_scalar('val/loss', self.test_loss, self.epoch)
            else:
                logger.info(f"Epoch {self.epoch} evaluation ends, accuracy {self.accu:.4f}")
                if self.accu > self.best_accu:
                    if self.cfg.SAVE:
                        logger.info(f"Save checkpoint, with {self.accu - self.best_accu:.4f} improvement")
                        self.graph.save(self.graph.save_path, self.graph.model, self.graph.sub_models, self.solvers, self.epoch, self.accu)                    
                    self.best_accu = self.accu
                self.visualizer.add_scalar('val/accuracy', self.accu, self.epoch)

    def _train_once(self):
        raise NotImplementedError

    def Train(self):
        self._start()
        while self.epoch < self.max_epoch:
            self._train_epoch_start()
            self._train_once()
            self._train_epoch_end()
            
            if self.epoch % self.cfg.EVALUATE_FREQ == 0:
                if self.cfg.DISTRIBUTED:
                    if dist.get_rank() == 0:
                        self._evaluate()
                else:
                    self._evaluate()

            if self.cfg.SOLVER.LR_POLICY == 'plateau' and self.cfg.SOLVER.MIN_LR >= self.solvers['model'].monitor_lr:
                logger.info(f"LR {self.solvers['model'].monitor_lr} is less than {self.cfg.SOLVER.MIN_LR}")
                break
        logger.info(f"Best accuracy {self.best_accu:.2f}")

    def Inference(self):
        raise NotImplementedError

    def _evaluate(self):
        raise NotImplementedError        

    @staticmethod
    def tensor_to_scalar(tensor):
        if isinstance(tensor, list):
            scalar = []
            for _tensor in tensor:
                scalar.append(_tensor.item())
        elif isinstance(tensor, dict):
            scalar = {}
            for _tensor in tensor:
                scalar[_tensor] = tensor[_tensor].item()
        elif isinstance(tensor, torch.Tensor) and tensor.dim() != 0:
            if tensor.is_cuda:
                scalar = tensor.cpu().detach().numpy().tolist()
            else:
                scalar = tensor.detach().numpy().tolist()
        else:
            scalar = tensor.item()
        return scalar

    

class data_prefetcher():
    def __init__(self, loader):
        self.loader = iter(loader)
        self.stream = torch.cuda.Stream()
        # With Amp, it isn't necessary to manually convert data to half.
        # if args.fp16:
        #     self.mean = self.mean.half()
        #     self.std = self.std.half()
        self.preload()

    def preload(self):
        try:
            self.next_batch = next(self.loader)
        except StopIteration:
            self.next_batch = None
            return
        # if record_stream() doesn't work, another option is to make sure device inputs are created
        # on the main stream.
        if isinstance(self.next_batch, list):
            self.next_batch_gpu = []
            for i in range(len(self.next_batch)):
                self.next_batch_gpu.append(torch.empty_like(self.next_batch[i], device='cuda'))
            # Need to make sure the memory allocated for next_* is not still in use by the main stream
            # at the time we start copying to next_*:
            self.stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(self.stream):
                # more code for the alternative if record_stream() doesn't work:
                # copy_ will record the use of the pinned source tensor in this side stream.
                for i in range(len(self.next_batch)):
                    self.next_batch_gpu[i].copy_(self.next_batch[i], non_blocking=True)
                self.next_batch = self.next_batch_gpu

                # With Amp, it isn't necessary to manually convert data to half.
                # if args.fp16:
                #     self.next_input = self.next_input.half()
                # else:
        elif isinstance(self.next_batch, dict):
            self.next_batch_gpu = {}
            for key in self.next_batch.keys():
                self.next_batch_gpu[key] = torch.empty_like(self.next_batch[key], device='cuda')
            # Need to make sure the memory allocated for next_* is not still in use by the main stream
            # at the time we start copying to next_*:
            self.stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(self.stream):
                # more code for the alternative if record_stream() doesn't work:
                # copy_ will record the use of the pinned source tensor in this side stream.
                for key in self.next_batch.keys():
                    self.next_batch_gpu[key].copy_(self.next_batch[key], non_blocking=True)
                self.next_batch = self.next_batch_gpu

                # With Amp, it isn't necessary to manually convert data to half.
                # if args.fp16:
                #     self.next_input = self.next_input.half()
                # else:
        else:
            raise TypeError
    def next(self):
        torch.cuda.current_stream().wait_stream(self.stream)
        batch = self.next_batch
        if isinstance(batch, list):
            if batch is not None:
                for i in range(len(batch)):
                    batch[i].record_stream(torch.cuda.current_stream())
        elif isinstance(self.next_batch, dict):
            if batch is not None:
                for key in batch.keys():
                    batch[key].record_stream(torch.cuda.current_stream())
        else:
            raise TypeError

        self.preload()
        return batch
