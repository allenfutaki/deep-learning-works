import math

import torch
import torch.nn as nn
import torch.nn.functional as F
from tools.utils import _tranpose_and_gather_feat, bbox_overlaps_ciou
from tools.scopehead_utils import scopehead_det_decode

class L1Loss(nn.Module):
    def __init__(self, loss_type='l1'):
        super(L1Loss, self).__init__()
        if loss_type == 'l1':
            self.loss = F.l1_loss
        elif loss_type == 'smooth':
            self.loss = F.smooth_l1_loss
        else:
            raise TypeError
    
    def forward(self, output, mask, ind, target):
        pred = _tranpose_and_gather_feat(output, ind)
        mask = mask.unsqueeze(2).expand_as(pred).float()
        loss = self.loss(pred * mask, target * mask, reduction='sum')
        loss = loss / (mask.sum() + 1e-4)
        return loss

class FocalLoss(nn.Module):
    ''' Modified focal loss. Exactly the same as CornerNet.
        Runs faster and costs a little bit more memory
      Arguments:
        pred (batch x c x h x w)
        gt_regr (batch x c x h x w)
    '''
    def __init__(self, a=2, b=4):
        super(FocalLoss, self).__init__()
        self.a = a
        self.b = b
        self.basic_loss = nn.BCEWithLogitsLoss(reduction="none")

    def forward(self, feat, target):
        pos_inds = target.eq(1) 
        # neg_inds = target.eq(0)
        nonig_inds = target.gt(-1)

        log_loss = self.basic_loss(feat, target)
        alpha = torch.ones_like(target) * self.b
        alpha = torch.where(target.eq(1), alpha, 1 - alpha)
        focal_weight = torch.where(target.eq(1), 1 - feat, feat)
        focal_weight = alpha * torch.pow(focal_weight, self.a)
        focal_loss = focal_weight[nonig_inds] * log_loss[nonig_inds]
        return focal_loss.sum() / max(1.0, pos_inds.sum())

        # pos_weight = torch.pow(1 - feat[pos_inds], self.a)
        # pos_weight = torch.pow(1 - feat[pos_inds], self.a) * self.b
        # neg_weight = torch.pow(feat[neg_inds], self.a) * torch.pow(1 - target[neg_inds], self.b)
        # neg_weight = torch.pow(feat[neg_inds], self.a) * (1 - self.b)
        # focal_loss = (pos_weight * log_loss[pos_inds]).sum() + (neg_weight * log_loss[neg_inds]).sum()        
        # return focal_loss / max(1.0, pos_inds.sum())
        # pos_inds = target.eq(1).float()
        # neg_inds = target.lt(1).float()

        # log_loss = self.basic_loss(feat, pos_inds)
        # pos_weight = pos_inds * torch.pow(1 - feat, self.a)
        # neg_weight = neg_inds * torch.pow(feat, self.a) * torch.pow(1 - target, self.b)
        # focal_weight = pos_weight + neg_weight

        # num_pos  = pos_inds.sum()
        # return torch.sum(focal_weight*log_loss) / max(1.0, num_pos)

class CrossEntropyLossLS(nn.Module):
    """Cross entropy loss with label smoothing regularizer.

    Reference:
    Szegedy et al. Rethinking the Inception Architecture for Computer Vision. CVPR 2016.
    Equation: y = (1 - epsilon) * y + epsilon / K.

    Args:
        num_classes (int): number of classes.
        epsilon (float): weight.
    """
    def __init__(self, num_classes, epsilon=0.1):
        super(CrossEntropyLossLS, self).__init__()
        self.num_classes = num_classes
        self.epsilon = epsilon
        self.logsoftmax = nn.LogSoftmax(dim=1)

    def forward(self, inputs, targets):
        """
        Args:
            inputs: prediction matrix (before softmax) with shape (batch_size, num_classes)
            targets: ground truth labels with shape (num_classes)
        """
        device = inputs.get_device()
        log_probs = self.logsoftmax(inputs)
        targets = torch.zeros(log_probs.size()).scatter_(1, targets.unsqueeze(1).data.cpu(), 1)
        if device > -1: targets = targets.cuda()
        targets = (1 - self.epsilon) * targets + self.epsilon / self.num_classes
        loss = (- targets * log_probs).mean(0).sum()
        return loss

class TripletLoss(object):
    """Modified from Tong Xiao's open-reid (https://github.com/Cysu/open-reid).
    Related Triplet Loss theory can be found in paper 'In Defense of the Triplet
    Loss for Person Re-Identification'."""

    def __init__(self, margin=None):
        self.margin = margin
        if margin is not None:
            self.ranking_loss = nn.MarginRankingLoss(margin=margin)
        else:
            self.ranking_loss = nn.SoftMarginLoss()

    def __call__(self, global_feat, labels, normalize_feature=True):
        if normalize_feature:
            global_feat = self._normalize(global_feat, axis=-1)
        dist_mat = self._euclidean_dist(global_feat, global_feat)
        dist_ap, dist_an = self._hard_example_mining(
            dist_mat, labels)
        y = dist_an.new().resize_as_(dist_an).fill_(1)
        if self.margin is not None:
            loss = self.ranking_loss(dist_an, dist_ap, y)
        else:
            loss = self.ranking_loss(dist_an - dist_ap, y)
        return loss, dist_ap, dist_an

    def _normalize(self, x, axis=-1):
        """Normalizing to unit length along the specified dimension.
        Args:
        x: pytorch Variable
        Returns:
        x: pytorch Variable, same shape as input
        """
        x = 1. * x / (torch.norm(x, 2, axis, keepdim=True).expand_as(x) + 1e-12)
        return x

    def _euclidean_dist(self, x, y):
        """
        Args:
        x: pytorch Variable, with shape [m, d]
        y: pytorch Variable, with shape [n, d]
        Returns:
        dist: pytorch Variable, with shape [m, n]
        """
        m, n = x.size(0), y.size(0)
        xx = torch.pow(x, 2).sum(1, keepdim=True).expand(m, n)
        yy = torch.pow(y, 2).sum(1, keepdim=True).expand(n, m).t()
        dist = xx + yy
        dist.addmm_(1, -2, x, y.t())
        dist = dist.clamp(min=1e-12).sqrt()  # for numerical stability
        return dist

    def _hard_example_mining(self, dist_mat, labels, return_inds=False):
        """For each anchor, find the hardest positive and negative sample.
        Args:
        dist_mat: pytorch Variable, pair wise distance between samples, shape [N, N]
        labels: pytorch LongTensor, with shape [N]
        return_inds: whether to return the indices. Save time if `False`(?)
        Returns:
        dist_ap: pytorch Variable, distance(anchor, positive); shape [N]
        dist_an: pytorch Variable, distance(anchor, negative); shape [N]
        p_inds: pytorch LongTensor, with shape [N];
            indices of selected hard positive samples; 0 <= p_inds[i] <= N - 1
        n_inds: pytorch LongTensor, with shape [N];
            indices of selected hard negative samples; 0 <= n_inds[i] <= N - 1
        NOTE: Only consider the case in which all labels have same num of samples,
        thus we can cope with all anchors in parallel.
        """

        assert len(dist_mat.size()) == 2
        assert dist_mat.size(0) == dist_mat.size(1)
        N = dist_mat.size(0)

        # shape [N, N]
        is_pos = labels.expand(N, N).eq(labels.expand(N, N).t())
        is_neg = labels.expand(N, N).ne(labels.expand(N, N).t())

        # `dist_ap` means distance(anchor, positive)
        # both `dist_ap` and `relative_p_inds` with shape [N, 1]
        dist_ap, relative_p_inds = torch.max(
            dist_mat[is_pos].contiguous().view(N, -1), 1, keepdim=True)
        # `dist_an` means distance(anchor, negative)
        # both `dist_an` and `relative_n_inds` with shape [N, 1]
        dist_an, relative_n_inds = torch.min(
            dist_mat[is_neg].contiguous().view(N, -1), 1, keepdim=True)
        # shape [N]
        dist_ap = dist_ap.squeeze(1)
        dist_an = dist_an.squeeze(1)

        if return_inds:
            # shape [N, N]
            ind = (labels.new().resize_as_(labels)
                .copy_(torch.arange(0, N).long())
                .unsqueeze(0).expand(N, N))
            # shape [N, 1]
            p_inds = torch.gather(
                ind[is_pos].contiguous().view(N, -1), 1, relative_p_inds.data)
            n_inds = torch.gather(
                ind[is_neg].contiguous().view(N, -1), 1, relative_n_inds.data)
            # shape [N]
            p_inds = p_inds.squeeze(1)
            n_inds = n_inds.squeeze(1)
            return dist_ap, dist_an, p_inds, n_inds

        return dist_ap, dist_an

class CenterLoss(nn.Module):
    r"""Implement of center loss: :
    Args:
        in_features: size of features
        num_classes: number of identity in dataset
    """
    def __init__(self, in_features, num_classes):
        super(CenterLoss, self).__init__()
        self.center = nn.Parameter(torch.randn(num_classes, in_features))

    def forward(self, inputs, labels):
        device = inputs.get_device()
            
        n = inputs.size(0)
        m = self.center.size(0)  

        #  cdist = F.linear(inputs, center_feature)         
        cdist = torch.pow(inputs, 2).sum(dim=1, keepdim=True).expand(n, m) + \
             torch.pow(self.center, 2).sum(dim=1, keepdim=True).expand(m ,n).t()
        cdist.addmm_(1, -2, inputs, self.center.t())        

        target = labels.view(-1,1).long()
        p = torch.zeros(cdist.size())

        if device > -1:
            target = target.to(device)
            p = p.to(device)

        p.scatter_(1, target, 1)

        center_loss = cdist[p==1].clamp(min = 1e-12, max = 1e+12).mean()
        
        return center_loss

class SoftTripletLoss(nn.Module):
    """
    Implement Attention Network Robustification for Person ReID (https://arxiv.org/abs/1910.07038)
    Eq(5).
    """
    def __init__(self):
        super(SoftTripletLoss, self).__init__()

    def forward(self, norm_feats, labels):
        dist_mat = self._euclidean_dist(norm_feats, norm_feats)
        N = dist_mat.size(0)
        pos_mask = labels.expand(N, N).eq(labels.expand(N, N).t())
        neg_mask = labels.expand(N, N).ne(labels.expand(N, N).t())
        d_ap = dist_mat[pos_mask]
        d_np = dist_mat[neg_mask]
        pos_w = F.softmax(d_ap, dim=0)
        neg_w = F.softmax(-1*d_np, dim=0)
        loss = self.softplus(torch.dot(pos_w, d_ap) - torch.dot(neg_w, d_np))
        return loss

    def _euclidean_dist(self, x, y):
        """
        Args:
        x: pytorch Variable, with shape [m, d]
        y: pytorch Variable, with shape [n, d]
        Returns:
        dist: pytorch Variable, with shape [m, n]
        """
        m, n = x.size(0), y.size(0)
        xx = torch.pow(x, 2).sum(1, keepdim=True).expand(m, n)
        yy = torch.pow(y, 2).sum(1, keepdim=True).expand(n, m).t()
        dist = xx + yy
        dist.addmm_(1, -2, x, y.t())
        dist = dist.clamp(min=1e-12).sqrt()  # for numerical stability
        return dist

    @staticmethod
    def softplus(x):
        return torch.log(1+torch.exp(x))

class AMSoftmaxWithLoss(nn.Module):
    r"""Implement of large margin cosine distance in cross entropy with label smoothing: :
    Args:
        in_features: size of each input sample
        num_classes: number of identity in dataset
        s: norm of input feature
        m: margin
        cos(theta) - m
    """

    def __init__(self, s=30, m=0.35, relax=0.0):
        super(AMSoftmaxWithLoss, self).__init__()
        self.s = s
        self.m = m
        self.relax = relax
        self.ce = nn.CrossEntropyLoss(reduction='none', ignore_index=-1)

    def forward(self, cosine, labels):
        device = cosine.get_device()

        phi = cosine - self.m

        one_hot = torch.zeros(cosine.size())
        target = labels.view(-1,1).long()

        if device > -1:
            one_hot = one_hot.to(device)
            target = target.to(device)
           
        one_hot.scatter_(1, target, 1)

        output = (one_hot * phi) + ((1.0 - one_hot) * cosine)
        output *= self.s
        loss = self.ce(output, labels)
        if self.relax > 0.0:
            log_logit = F.log_softmax(output, dim=1)
            logit = torch.exp(log_logit)
            loss = F.relu(loss + self.relax * (logit * log_logit).sum(1))
            with torch.no_grad():
                nonzero_count = loss.nonzero().size(0)
            if nonzero_count > 0:
                return loss.sum() / nonzero_count
            else:
                return loss.sum()

        return loss.mean()

class BinCrossEntropyLoss(nn.Module):
    def __init__(self):
        super(BinCrossEntropyLoss, self).__init__()
        self.loss = nn.BCEWithLogitsLoss(reduction='sum')
    def forward(self, output, mask, ind, target):
        # output : N x (2 x num_bins) x W x H
        # target : N x max_obj x 2
        pred = _tranpose_and_gather_feat(output, ind) # N x max_obj x (2 x num_bins)
        mask = mask.unsqueeze(2).expand_as(pred).bool()
        # target_mask = mask.unsqueeze(2).expand_as(target).bool()
        masked_pred = pred[mask].view(-1)
        masked_target = target[mask]
        loss = self.loss(masked_pred, masked_target) / (mask.sum() + 1e-4)
        return loss

class CIOULoss(nn.Module):
    '''
    Reference
         Complete-IOU loss ( https://arxiv.org/abs/1911.08287 )
    Code
        https://github.com/Zzh-tju/DIoU-SSD-pytorch/blob/master/utils/box/box_utils.py#L47
    Args
        p_wh : prediced wh in centernet
        p_reg : prediced reg in centernet
        t_inds : ind generated by scopehead or centernet
        t_hm : hm generated by scopehead or centernet
        t_dets : grountruth of bboxes
    '''

    def __init__(self):
        super(CIOULoss, self).__init__()

    def forward(self, p_wh, p_reg, mask, t_inds, t_dets):
        device = p_wh.get_device()
        _, _, h, w = p_wh.size()
        p_reg = _tranpose_and_gather_feat(p_reg, t_inds)
        p_wh = _tranpose_and_gather_feat(p_wh, t_inds)
        p_reg = p_reg[mask>0,:]
        p_wh = p_wh[mask>0,:].view(-1, 2)

        t_dets = torch.cat(t_dets, dim=0).to(device)
        t_dets = t_dets[t_dets[:,-1] > 0]
        t_dets[:,[0, 2]] *= w
        t_dets[:,[1, 3]] *= h

        cx = ((t_dets[:,0] + t_dets[:,2]) / 2).int() + p_reg[:, 0]
        cy = ((t_dets[:,1] + t_dets[:,3]) / 2).int() + p_reg[:, 1]
    
        p_dets = torch.stack([cx - p_wh[..., 0] / 2, 
                              cy - p_wh[..., 1] / 2,
                              cx + p_wh[..., 0] / 2, 
                              cy + p_wh[..., 1] / 2], dim=-1).view(-1, 4)
        if p_dets.dtype == torch.float16:
            t_dets = t_dets.half()
        ciou = bbox_overlaps_ciou(p_dets, t_dets)
        return (1 - ciou).mean()
    
class FsafCIOULoss(nn.Module):
    '''
    Reference
         Complete-IOU loss ( https://arxiv.org/abs/1911.08287 )
    Code
        https://github.com/Zzh-tju/DIoU-SSD-pytorch/blob/master/utils/box/box_utils.py#L47
    Args
        p_wh : prediced wh in centernet
        p_reg : prediced reg in centernet
        t_inds : ind generated by scopehead or centernet
        t_hm : hm generated by scopehead or centernet
        t_dets : grountruth of bboxes
    '''

    def __init__(self):
        super(FsafCIOULoss, self).__init__()

    def forward(self, p_wh, p_reg, mask, count, t_inds, t_dets):
        device = p_wh.get_device()
        _, _, h, w = p_wh.size()
        p_reg = _tranpose_and_gather_feat(p_reg, t_inds)
        p_wh = _tranpose_and_gather_feat(p_wh, t_inds)
        p_reg = p_reg[mask>0,:]
        p_wh = p_wh[mask>0,:].view(-1, 4)

        t_dets = torch.cat(t_dets, dim=0).to(device)
        t_dets = t_dets[t_dets[:,-1] > 0]
        t_dets[:,[0, 2]] *= w
        t_dets[:,[1, 3]] *= h
        t_dets = t_dets.repeat_interleave(repeats=count[count>0], dim=0)

        cx = ((t_dets[:,0] + t_dets[:,2]) / 2).int() + p_reg[:, 0]
        cy = ((t_dets[:,1] + t_dets[:,3]) / 2).int() + p_reg[:, 1]
    
        p_dets = torch.stack([cx - p_wh[..., 0], 
                              cy - p_wh[..., 1],
                              cx + p_wh[..., 2], 
                              cy + p_wh[..., 3]], dim=-1).view(-1, 4)
        if p_dets.dtype == torch.float16:
            t_dets = t_dets.half()
        ciou = bbox_overlaps_ciou(p_dets, t_dets)
        return (1 - ciou).mean()

if __name__ == "__main__":
    feats = torch.rand(16, 10)
    labels = torch.Tensor([0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3])
    norm_feats = F.normalize(feats)
    loss = SoftTripletLoss()
    output = loss(norm_feats, labels)
    print(output)
