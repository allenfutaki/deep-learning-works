import argparse
import sys
from tqdm import tqdm
import torch
import torch.nn.functional as F
from tools.eval_par_metrics import eval_par_accuracy
from model.OSNetv2 import osnet_x1_0
from config.config_manager import _C as cfg
from data.build_loader import build_par_loader
from model.managers.manager_par import PARManager
from tools.logger import setup_logger
from engine.engines.engine_par import PAREngine
import numpy as np
from tools.logger import setup_logger



parser = argparse.ArgumentParser(description="PyTorch Template MNIST Training")
parser.add_argument("--config_file", default="", help="path to config file", type=str)
parser.add_argument("--opts", help="Modify config options using the command-line", default=None,
                    nargs=argparse.REMAINDER)
parser.add_argument("--type", default="roc", help="evaluation mode", type=str)
parser.add_argument("--cache", default="", help="evaluation type", type=str)

args = parser.parse_args()

if args.config_file != "":
    cfg.merge_from_file(args.config_file)
if args.opts != None:
    cfg.merge_from_list(args.opts)

log_name = "{}_evaluation_{}_{}".format(args.type, cfg.DATASET.NAME, cfg.EVALUATE.split("/")[-1])
logger = setup_logger("./evaluation/", log_name)
logger.info("Running with config:\n{}".format(cfg))

action = input("Config Confirmed ? (Y/N)").lower().strip()
if action == 'y':
    
    use_gpu = True 

    model_manager = PARManager(cfg)
    core = model_manager.model     

    core = core.cuda()
    core.eval()

    _, vdata = build_par_loader(cfg)

    outputs = []
    targets = []
    with torch.no_grad():
        for batch in tqdm(self.vdata, desc="Validation"):
            
            images, target = batch
            if self.use_gpu: images = images.cuda()
            
            output = self.core(images)
            outputs.append(output.cpu())
            targets.append(target)
    
    pt = torch.cat(outputs, 0)
    gt = torch.cat(targets, 0)
    
    TPR, FPR, total_precision = eval_par_accuracy(pt.numpy(), gt.numpy())

    self.accu = total_precision[50]

    logger.info("Computing Prec and Recall")
    logger.info("Results ----------")
    logger.info("ROC curve")
    for thresh in [0, 25, 50, 75]:
        logger.info("Threshold: {:<3}  |  Precision: {:.2f}  |  TPR: {:.2f}  |  FPR: {:.2f}".format(thresh*0.01, total_precision[thresh], TPR[thresh], FPR[thresh]))
    logger.info("------------------")

 
else:
    sys.exit(1)