from src.database.data import *
import os
import os.path as osp
import pycocotools.coco as coco
import json
from collections import defaultdict

class COCO(BaseData):
    def __init__(self, path="", branch="", coco_target="", 
                num_keypoints=-1, num_classes=-1, output_strides=-1, 
                use_train=False, use_test=False, **kwargs):
        '''
        COCO type data: image and label stored in coco format json
        Args:
            path: path to all data
            branch: name of data, e.g., coco_person_kp or coco
            coco_target: only use one of categories in data, e.g., person in COCO
        '''
        super().__init__()
        self.dataset_dir = osp.join(path, branch)
        self.category = coco_target
        self.train_dir   = osp.join(self.dataset_dir, "train2017")
        self.train_anno  = osp.join(self.dataset_dir, self.category, "instances_train2017.json")
        self.val_dir     = osp.join(self.dataset_dir, "val2017")
        self.val_anno    = osp.join(self.dataset_dir, self.category, "instances_val2017.json")
        self._check_before_run()
         
        if use_train:
            train_coco, train_images, train_num_samples, train_pid2label, train_num_person = self._process_dir(self.train_anno, self.train_dir, split='train')
            self.train['handle'] = train_coco
            self.train['pid'] = train_pid2label
            self.train['indice'] = train_images
            self.train['n_samples'] = train_num_samples
            self.train['num_keypoints'] = num_keypoints
            self.train['num_classes'] = num_classes
            self.train['num_person'] = train_num_person
            self.train['strides'] = output_strides                   
            logger.info(f"=> {branch.upper()} TRAIN is loaded")
            logger.info("  Dataset statistics:")
            logger.info("  -----------------------------")
            logger.info("  subset   | #id     | # images")
            logger.info("  -----------------------------")
            logger.info(f"  train    |{train_num_person:>8} | {train_num_samples:>8}")
            logger.info("  -----------------------------")
        if use_test:
            val_coco, val_images, val_num_samples, val_pid2label, val_num_person = self._process_dir(self.val_anno, self.val_dir, split='val')
            self.val['handle'] = val_coco  
            self.val['pid'] = val_pid2label      
            self.val['indice'] = val_images
            self.val['n_samples'] = val_num_samples
            self.val['num_keypoints'] = num_keypoints
            self.val['num_classes'] = num_classes           
            self.val['num_person'] = val_num_person
            self.val['strides'] = output_strides                  
            logger.info(f"=> {branch.upper()} VAL is loaded")
            logger.info("  Dataset statistics:")
            logger.info("  -----------------------------")
            logger.info("  subset   | #id     | # images")
            logger.info("  -----------------------------")
            logger.info(f"  val    |{val_num_person:>8} | {val_num_samples:>8}")
            logger.info("  -----------------------------")
        
    def _check_before_run(self):
        """Check if all files are available before going deeper"""
        if not osp.exists(self.dataset_dir):
            raise RuntimeError("'{}' is not available".format(self.dataset_dir))
        if not osp.exists(self.train_dir):
            raise RuntimeError("'{}' is not available".format(self.train_dir))
        if not osp.exists(self.train_anno):
            orig_json = osp.join(self.dataset_dir, 'original', "instances_train2017.json")
            if not osp.exists(orig_json):
                raise RuntimeError("'{}' is not available to make target of coco".format(orig_json))
            self._make_target_coco(orig_json, self.train_anno, self.category)
        if not osp.exists(self.val_dir):
            raise RuntimeError("'{}' is not available".format(self.val_dir))        
        if not osp.exists(self.val_anno):
            orig_json = osp.join(self.dataset_dir, 'original', "instances_val2017.json")
            if not osp.exists(orig_json):
                raise RuntimeError("'{}' is not available to make target of coco".format(orig_json))
            self._make_target_coco(orig_json, self.val_anno, self.category)
    
    def _process_dir(self, anno_path, img_path, split='train'):
        data_handle = coco.COCO(anno_path)
        image_ids = data_handle.getImgIds()
        pids = defaultdict(int)
        images = []
        for img_id in image_ids:
            idxs = data_handle.getAnnIds(imgIds=[img_id])
            if len(idxs) > 0:
                if split == 'train':
                    fname = data_handle.loadImgs(ids=[img_id])[0]['file_name']
                    fname = osp.join(img_path, fname)
                    images.append((img_id, fname))
                    anns = data_handle.loadAnns(ids=idxs)
                    for ann in anns:
                        if 'pid' not in ann or ann['pid'] == '-1':
                            continue
                        pids[f"{int(ann['pid']):05}"] += 1
                else:
                    fname = data_handle.loadImgs(ids=[img_id])[0]['file_name']
                    fname = osp.join(img_path, fname)
                    images.append((img_id, fname))
     
        num_samples = len(images)
        
        pid2label = {}
        label = 0
        _pids = sorted(list(pids.keys()))
        for pid in _pids:
            if pids[pid] >= 4:
                pid2label[str(int(pid))] = label
                label += 1
            else:
                pid2label[str(int(pid))] = -1
        pid2label['-1'] = -1
        num_person = len(pid2label) - 1
        
        return data_handle, images, num_samples, pid2label, num_person
    
    def _make_target_coco(self, src, dst, category):
        logger.info("Making target of coco of {} ...".format(dst))
        if not osp.exists(osp.dirname(dst)):
            os.mkdir(osp.dirname(dst))
        anno_path = src

        f = open(anno_path, 'r')
        coco_json = json.loads(f.readline())
        coco_trt = {}
        coco_trt['info'] = coco_json['info']
        coco_trt['licenses'] = coco_json['licenses']
        coco_trt['categories'] = [coco_json['categories'][0]]

        del coco_json

        coco_data = coco.COCO(anno_path)
        cat_ids = coco_data.getCatIds(catNms=[category])
        trt_img_ids = coco_data.getImgIds(catIds=cat_ids)
        annids = coco_data.getAnnIds(imgIds=trt_img_ids)
        anns = coco_data.loadAnns(ids=annids)
        trt_anns = []
        for ann in anns:
            if ann['category_id'] == cat_ids[0]:
                trt_anns.append(ann)
        trt_imgs = coco_data.loadImgs(ids=trt_img_ids)
        
        coco_trt['images'] = trt_imgs
        coco_trt['annotations'] = trt_anns

        json.dump(coco_trt, open(dst, 'w'))