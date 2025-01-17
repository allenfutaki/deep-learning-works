from src.engine import *
from tqdm import tqdm
from tools.eval_reid_metrics import evaluate, eval_recall

# recover = T.Compose([T.Normalize(mean = [-0.485/0.229, -0.456/0.224, -0.406/0.225], std = [1/0.229,1/0.224,1/0.225])])

class TrickReIDEngine(BaseEngine):
    def __init__(self, cfg, graph, loader, solvers, visualizer):
        super(TrickReIDEngine, self).__init__(cfg, graph, loader, solvers, visualizer)

    def _train_iter_end(self): 
        self.loss.backward() 
        self.solvers['main'].step()

        self.loss = self.tensor_to_scalar(self.loss)
        self.losses = self.tensor_to_scalar(self.losses)     
        if self.cfg.IO:
            self.visualizer.add_scalar('train/loss', self.loss, self.iter)              
            for loss in self.losses:
                self.visualizer.add_scalar(f'train/loss/{loss}', self.losses[loss], self.iter)
            self.visualizer.add_scalar('train/accuracy', self.train_accu, self.iter)   
            for solver in self.solvers:
                self.visualizer.add_scalar(f'train/solver/{solver}/lr', self.solvers[solver].monitor_lr, self.iter)

    def _train_once(self):
        accus = []   
        for batch in tqdm(self.tdata, desc=f"TRAIN[{self.epoch}/{self.cfg.SOLVER.MAX_EPOCHS}]"):
            self._train_iter_start()
            if self.use_gpu:
                for key in batch:
                    batch[key] = batch[key].cuda()
            outputs = self.graph.run(batch['inp']) 
            self.loss, self.losses = self.graph.loss_head(outputs, batch)
            accus.append((outputs['global'].max(1)[1] == batch['pid']).float().mean())        
            self._train_iter_end()

            for sub_model in self.graph.sub_models:
                for param in self.graph.sub_models[sub_model].parameters():
                    param.grad.data *= (1. / self.cfg.REID.CENTER_LOSS_WEIGHT)
            self.solvers['center'].step()

        self.train_accu = self.tensor_to_scalar(torch.stack(accus).mean())

    def _evaluate(self, eval=False):
        logger.info("Epoch {} evaluation start".format(self.epoch))
        title = "EVALUATE" if eval else f"TEST[{self.epoch}/{self.cfg.SOLVER.MAX_EPOCHS}]"
        accus = []        
        with torch.no_grad():
            self._eval_epoch_start()
            qf, q_pids, q_camids = [], [], []
            for batch in tqdm(self.qdata, desc=title): 
                imgs, pids, camids = batch['inp'], batch['pid'], batch['camid']
                features = self.graph.run(imgs.cuda() if self.use_gpu else imgs)['neck']
                features = F.normalize(features)
                qf.append(features.cpu())
                q_pids.extend(pids)
                q_camids.extend(camids)

            qf = torch.cat(qf, 0)
            q_pids = np.asarray(q_pids)
            q_camids = np.asarray(q_camids)
            logger.info("Extracted features for query set, obtained {}-by-{} matrix".format(qf.size(0), qf.size(1)))

            gf, g_pids, g_camids = [], [], []
            for batch in tqdm(self.gdata, desc=title): 
                imgs, pids, camids = batch['inp'], batch['pid'], batch['camid']
                features = self.graph.run(imgs.cuda() if self.use_gpu else imgs)['neck']
                features = F.normalize(features)
                gf.append(features.cpu())
                g_pids.extend(pids)
                g_camids.extend(camids)

            gf = torch.cat(gf, 0)
            g_pids = np.asarray(g_pids)
            g_camids = np.asarray(g_camids)
            logger.info("Extracted features for gallery set, obtained {}-by-{} matrix".format(gf.size(0), gf.size(1)))

        distmat =  1 - F.linear(qf, gf)
        distmat = distmat.numpy()

        logger.info("Computing CMC and mAP")
        cmc, mAP = evaluate(distmat, q_pids, g_pids, q_camids, g_camids)

        logger.info("Results ----------")
        logger.info("mAP: {:.1%}".format(mAP))
        logger.info("CMC curve")
        for r in [1, 5, 10, 20]:
            logger.info("Rank-{:<3}: {:.1%}".format(r, cmc[r - 1]))
        logger.info("------------------")

        logger.info("Computing Recall")
        rs, confs, gts, fg = eval_recall(distmat, q_pids, g_pids, q_camids, g_camids)

        logger.info("Results ------------: {:>4} / {:>4} / {:>4}".format("Q0.5", "Q0.75", "Q0.95"))
        logger.info("Number of candidates: {:.2f} / {} / {}".format(np.quantile(rs, q = 0.5), np.quantile(rs, q = 0.75), np.quantile(rs, q = 0.95)))
        logger.info("          Confidence: {:.2f} / {:.2f} / {:.2f}".format(np.quantile(confs, q = 0.5), np.quantile(confs, q = 0.75), np.quantile(confs, q = 0.95)))
        logger.info("    Number of target: {:.2f} / {} / {}".format(np.quantile(gts, q = 0.5), np.quantile(gts, q = 0.75), np.quantile(gts, q = 0.95)))  
        logger.info("------------------")
        
        if eval:
            np.save(self.cfg.OUTPUT_DIR+"/rs.npy", rs)
            np.save(self.cfg.OUTPUT_DIR+"/confs.npy", confs)
            np.save(self.cfg.OUTPUT_DIR+"/gts.npy", gts)
            np.save(self.cfg.OUTPUT_DIR+"/filtered_gallery.npy", fg)
        else:
            self.accu = cmc[0]
            self._eval_epoch_end()

        del qf, gf, distmat
        
    def Evaluate(self):
        self._evaluate(eval=True)
        logger.info(self.accu)
