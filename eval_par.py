import argparse
import os
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
parser.add_argument("--config", default="", help="path to config file", type=str)
parser.add_argument("--opts", help="Modify config options using the command-line", default=None,
                    nargs=argparse.REMAINDER)
parser.add_argument("--type", default="roc", help="evaluation mode", type=str)
parser.add_argument("--cache", default="", help="evaluation type", type=str)

args = parser.parse_args()

if args.config != "":
    cfg.merge_from_file(args.config)
if args.opts != None:
    cfg.merge_from_list(args.opts)

log_name = "{}_evaluation_{}_{}".format(args.type, cfg.DATASET.NAME, cfg.EVALUATE.split("/")[-1])
if not os.path.exists("./evaluation/"):
    os.mkdir("./evaluation/")
logger = setup_logger("./evaluation/", log_name)
logger.info("Running with config:\n{}".format(cfg))

action = input("Config Confirmed ? (Y/N)").lower().strip()
if action == 'y':

    manager = PARManager(cfg)

    manager.use_multigpu()
    
    if os.path.exists("{}_pt.npy".format(args.cache)):
        logger.info("Loading from cache")
        pt = np.load("{}_pt.npy".format(args.cache))
        gt = np.load("{}_gt.npy".format(args.cache))
    else:
        use_gpu = True 

        core = manager.model     

        core = core.cuda()
        core.eval()

        _, vdata = build_par_loader(cfg)

        outputs = []
        targets = []
        with torch.no_grad():
            for batch in tqdm(vdata, desc="Validation"):
                
                images, target = batch
                if use_gpu: images = images.cuda()
                
                output = core(images)
                outputs.append(output.cpu())
                targets.append(target)
        
        pt = torch.cat(outputs, 0).numpy()
        gt = torch.cat(targets, 0).numpy()
        
        if args.cache:
            np.save("{}_pt.npy".format(args.cache), pt)
            np.save("{}_gt.npy".format(args.cache), gt)

    precs, recalls = eval_par_accuracy(pt, gt)
    t_precs = precs.mean(axis=1)
    t_recalls = recalls.mean(axis=1)
    logger.info("Computing Precision and Recall")
    logger.info("Results ----------")
    for thresh in [25, 50, 75]:
        logger.info("Threshold: {:5}".format(thresh*0.01))
        logger.info("{:10}  |  Precision: {:.2f}  |  Recall: {:.2f}".format("Total", t_precs[thresh], t_recalls[thresh]))
        for i, attr in enumerate(manager.category_names):
            if (i+1) not in cfg.PAR.IGNORE_CAT:
                logger.info("{:10}  |  Precision: {:.2f}  |  Recall: {:.2f}".format(attr, precs[thresh][i], recalls[thresh][i]))
        logger.info("##################")

    logger.info("------------------")

 
else:
    sys.exit(1)
