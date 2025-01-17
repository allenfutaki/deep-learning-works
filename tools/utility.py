import sys
import math
import logging

import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
from sklearn.cluster import DBSCAN
from tools.utils import dist_idx_to_pair_idx, pdist, _gather_feat, _tranpose_and_gather_feat
# from solver.solvers import *

def normalize(x, axis=-1):
    """Normalizing to unit length along the specified dimension.
    Args:
      x: pytorch Variable
    Returns:
      x: pytorch Variable, same shape as input
    """
    x = 1. * x / (torch.norm(x, 2, axis, keepdim=True).expand_as(x) + 1e-12)
    return x




class Flatten(nn.Module):
  def forward(self, input):
    return input.view(input.size(0), -1)

class ArcMarginProduct(nn.Module):
    r"""Implement of large margin arc distance: :
        Args:
            in_features: size of each input sample
            out_features: size of each output sample
            s: norm of input feature
            m: margin
            cos(theta + m)
        """
    def __init__(self, in_features, out_features, s=30.0, m=0.50, easy_margin=False):
        super(ArcMarginProduct, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.s = s
        self.m = m
        self.weight = nn.Parameter(torch.FloatTensor(out_features, in_features))
        nn.init.xavier_uniform_(self.weight)

        self.easy_margin = easy_margin
        self.cos_m = math.cos(m)
        self.sin_m = math.sin(m)
        self.th = math.cos(math.pi - m)
        self.mm = math.sin(math.pi - m) * m

    def forward(self, input, label):
        # --------------------------- cos(theta) & phi(theta) ---------------------------
        cosine = F.linear(F.normalize(input), F.normalize(self.weight))
        sine = torch.sqrt(1.0 - torch.pow(cosine, 2))
        phi = cosine * self.cos_m - sine * self.sin_m
        if self.easy_margin:
            phi = torch.where(cosine > 0, phi, cosine)
        else:
            phi = torch.where(cosine > self.th, phi, cosine - self.mm)
        # --------------------------- convert label to one-hot ---------------------------
        # one_hot = torch.zeros(cosine.size(), requires_grad=True, device='cuda')
        one_hot = torch.zeros(cosine.size(), device='cuda')
        one_hot.scatter_(1, label.view(-1, 1).long(), 1)
        # -------------torch.where(out_i = {x_i if condition_i else y_i) -------------
        output = (one_hot * phi) + ((1.0 - one_hot) * cosine)  # you can use torch.where if your torch.__version__ is 0.4
        output *= self.s
        # print(output)

        return output

class ConvFC(nn.Module):
    def __init__(self, in_planes, out_planes, bias=True):
        super(ConvFC, self).__init__()
        self.fc = nn.Conv2d(in_channels=in_planes, out_channels=out_planes, kernel_size=(1,1), stride=(1,1), padding=(0,0), groups=1, bias=bias)

    def forward(self, x):
        x = self.fc(x)
        return x

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



# class PushLoss(nn.Module):
#     r"""Implement of global push, center, push loss in RMNet: :
#     Args:
#         in_features: size of features
#         num_classes: number of identity in dataset
#         K: number of image per identity
#         m: margin
#         center, global push, push with size N, batch size
#     """
#     def __init__(self, in_features, num_classes):
#         super(PushLoss, self).__init__()
#         self.center = nn.Parameter(torch.randn(num_classes, in_features))
#         #  nn.init.xavier_uniform_(self.center)        

#     def forward(self, inputs, labels):
#         device = inputs.get_device()
            
#         n = inputs.size(0)
#         m = self.center.size(0)  

#         dist_mat = euclidean_dist(inputs, inputs)  
#         c_dist_mat = euclidean_dist(inputs, normalize(self.center))  
              

#         target = labels.view(-1,1).long()
#         p = torch.zeros(cdist.size())

#         if device > -1:
#             target = target.to(device)
#             p = p.to(device)

#         p.scatter_(1, target, 1)

#         center_loss = cdist[p==1].clamp(min = 1e-12, max = 1e+12).mean()
        
#         return center_loss

class TupletLoss(nn.Module):
    r"""Implement of global push, center, push loss in RMNet: :
    Args:
        m: margin
        thresh: similarity for hard negative
    """
    def __init__(self, m=0.3, thresh=0.6):
        super(TupletLoss, self).__init__()
        self.m = m
        self.thresh = thresh

    def forward(self, inputs, labels):
        device = inputs.get_device()

        n = inputs.size(0)
       
        dist = F.linear(inputs, inputs)

        target = labels.view(-1,1).long()
        rank_mask = 1 - torch.eye(n)
        mask = labels.expand(n, n).eq(labels.expand(n, n).t())

        if device > -1:
            target = target.to(device)
            rank_mask = rank_mask.to(device)
            
        true = mask[rank_mask==1].reshape(n,-1)
        rank1 = dist[rank_mask==1].reshape(n,-1).max(1)[1]
        match = true.gather(1, rank1.long().view(-1,1))

        push = []

        for i in range(n):
            dist_p = dist[i][mask[i]==1]
            dist_n = dist[i][mask[i]==0]  
            dist_n = dist_n[dist_n >= self.thresh]   
            rank = torch.exp(dist_n - dist_p.min() + self.m)
            push.append(torch.log(rank[rank > 1].sum() + 1) + 2 * torch.pow(dist_p - 1, 2).mean() / 2)

        push = torch.stack(push).mean()
        
        acc = match.float().mean()
        
        return push, acc.item()

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

    def __call__(self, global_feat, labels, normalize_feature=False):
        if normalize_feature:
            global_feat = normalize(global_feat, axis=-1)
        dist_mat = euclidean_dist(global_feat, global_feat)
        dist_ap, dist_an = hard_example_mining(
            dist_mat, labels)
        y = dist_an.new().resize_as_(dist_an).fill_(1)
        if self.margin is not None:
            loss = self.ranking_loss(dist_an, dist_ap, y)
        else:
            loss = self.ranking_loss(dist_an - dist_ap, y)
        return loss, dist_ap, dist_an

class AMSoftmax(nn.Module):
    r"""Implement of large margin cosine distance in cross entropy with label smoothing: :
    Args:
        in_features: size of each input sample
        num_classes: number of identity in dataset
        s: norm of input feature
        m: margin
        cos(theta) - m
    """

    def __init__(self, in_features, num_classes, s=30.0, m=0.30):
        super(AMSoftmax, self).__init__()
        self.s = s
        self.m = m

        self.weight = nn.Parameter(torch.FloatTensor(num_classes, in_features))
        nn.init.xavier_uniform_(self.weight)

    def forward(self, inputs, labels):
        device = inputs.get_device()

        cosine = F.linear(inputs, F.normalize(self.weight))
        phi = cosine - self.m

        one_hot = torch.zeros(cosine.size())
        target = labels.view(-1,1).long()

        if device > -1:
            one_hot = one_hot.to(device)
            target = target.to(device)
           
        one_hot.scatter_(1, target, 1)

        output = (one_hot * phi) + ((1.0 - one_hot) * cosine)
        output *= self.s

        return output

class PureConv1x1(nn.Module):
    """1x1 convolution"""
    
    def __init__(self, in_channels, out_channels, stride=1, groups=1):
        super(PureConv1x1, self).__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, 1, stride=stride, padding=0,
                              bias=False, groups=groups)        

    def forward(self, x):
        x = self.conv(x)        
        return x

class Conv1x1(nn.Module):
    """1x1 convolution + bn + relu."""
    
    def __init__(self, in_channels, out_channels, stride=1, groups=1):
        super(Conv1x1, self).__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, 1, stride=stride, padding=0,
                              bias=False, groups=groups)
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x

class Conv3x3(nn.Module):
    """3x3 convolution + bn + relu."""
    
    def __init__(self, in_channels, out_channels, stride=1, groups=1):
        super(Conv3x3, self).__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, 3, stride=stride, padding=1,
                              bias=False, groups=groups)
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x

class SpatialAttention(nn.Module):
    def __init__(self, in_channels, reduction=8):
        super(SpatialAttention, self).__init__()
        self.out_channels = in_channels // reduction
        self.scale = nn.Parameter(torch.FloatTensor([0]))
        self.conv_reduce1 = PureConv1x1(in_channels, self.out_channels)
        self.conv_reduce2 = PureConv1x1(in_channels, self.out_channels)
        self.conv = PureConv1x1(in_channels, in_channels)
        self.softmax = nn.Softmax(dim=2)
    def forward(self, x):
        n, c, h, w = x.shape
        # N x HW x C/r
        reduce_1 = self.conv_reduce1(x).view(n, self.out_channels, -1).transpose(1,2)
        # N x C/r x HW
        reduce_2 = self.conv_reduce2(x).view(n, self.out_channels, -1)
        # N x HW x HW
        A_s = self.softmax(torch.matmul(reduce_1, reduce_2))
        # N x C x HW
        multipler = self.conv(x).view(n, c, -1)
        A_s = torch.matmul(multipler, A_s)
        # N x C x HW
        weighted_A_s = torch.mul(A_s, self.scale)
        # N x C x H x W
        residual = x + weighted_A_s.view(n, c, h, w)
        return residual

class ChannelAttention(nn.Module):
    def __init__(self):
        super(ChannelAttention, self).__init__()
        self.scale = nn.Parameter(torch.FloatTensor([0]))
        self.softmax = nn.Softmax(dim=2)
    def forward(self, x):
        n, c, h, w = x.shape
        # N x C x HW
        x1 = x.view(n, c, -1) 
        # N x HW x C
        x2 = x.view(n, c, -1).transpose(1,2)
        # N x C x C
        A_s = self.softmax(torch.matmul(x1, x2))
        A_s = torch.matmul(A_s, x1)
        # N x C x HW
        weighted_A_s = torch.mul(A_s, self.scale)
        # N x C x H x W
        residual = x + weighted_A_s.view(n, c, h, w)
        return residual

class AttentionIncorporation(nn.Module):
    def __init__(self, in_channels, attention='sum'):
        super(AttentionIncorporation, self).__init__()

        self.attention = attention
        if attention == 's':
            self.spatial_attention = SpatialAttention(in_channels)
        elif attention == 'c':
            self.channel_attention = ChannelAttention()
        elif attention == 'sum':
            self.spatial_attention = SpatialAttention(in_channels)
            self.channel_attention = ChannelAttention()
        else:
            print("Not supported type")
            sys.exit(1)

    def forward(self, x):
        if self.attention == 's':
            spatial_attention_feat = self.spatial_attention(x)
            return spatial_attention_feat
        if self.attention == 'c':
            channel_attention_feat = self.channel_attention(x)
            return channel_attention_feat
        if self.attention == 'sum':
            spatial_attention_feat = self.spatial_attention(x)
            channel_attention_feat = self.channel_attention(x.contiguous())
            return spatial_attention_feat + channel_attention_feat

class AttentionConvBlock(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(AttentionConvBlock, self).__init__()
        self.conv1 = Conv3x3(in_channels, 512)
        self.conv2 = Conv1x1(512, out_channels)
        self.max_pool = nn.AdaptiveMaxPool2d(1)
    def forward(self, x):
        x = self.conv1(x)
        x = self.conv2(x)
        return x

class ClusterAssignment(nn.Module):
    def __init__(
            self,
            cluster_number,
            embedding_dimension,
            alpha=1.0,
            cluster_centers=None):
        """
        Module to handle the soft assignment, for a description see in 3.1.1. in Xie/Girshick/Farhadi,
        where the Student's t-distribution is used measure similarity between feature vector and each
        cluster centroid.

        :param cluster_number: number of clusters
        :param embedding_dimension: embedding dimension of feature vectors
        :param alpha: parameter representing the degrees of freedom in the t-distribution, default 1.0
        :param cluster_centers: clusters centers to initialise, if None then use Xavier uniform
        """
        super(ClusterAssignment, self).__init__()
        self.embedding_dimension = embedding_dimension
        self.cluster_number = cluster_number
        self.alpha = 1.0
        if cluster_centers is None:
            initial_cluster_centers = torch.zeros(
                self.cluster_number,
                self.embedding_dimension,
                dtype=torch.float
            )
            nn.init.xavier_uniform_(initial_cluster_centers)
        else:
            initial_cluster_centers = cluster_centers
        self.cluster_centers = nn.Parameter(initial_cluster_centers)

    def forward(self, batch: torch.Tensor) -> torch.Tensor:
        """
        Compute the soft assignment for a batch of feature vectors, returning a batch of assignments
        for each cluster.

        :param batch: FloatTensor of [batch size, embedding dimension]
        :return: FloatTensor [batch size, number of clusters]
        """
        norm_squared = torch.sum((batch.unsqueeze(1) - self.cluster_centers)**2, 2)
        numerator = 1.0 / (1.0 + (norm_squared / self.alpha))
        power = -float(self.alpha + 1) / 2
        numerator = numerator**power
        return (numerator.t() / torch.sum(numerator, 1)).t()

def get_self_label(dists, cycle):
    labels_list = []
    for i in range(len(dists)):
        if cycle==0:                
            ####DBSCAN cluster
            tri_mat = np.triu(dists[i],1)       # tri_mat.dim=2
            tri_mat = tri_mat[np.nonzero(tri_mat)] # tri_mat.dim=1
            tri_mat = np.sort(tri_mat,axis=None)
            top_num = np.round(1.6e-3*tri_mat.size).astype(int)
            eps = tri_mat[:top_num].mean()
            logger.info('eps in cluster: {:.3f}'.format(eps))
            cluster = DBSCAN(eps=eps,min_samples=4, metric='precomputed', n_jobs=8)
            cluster_list.append(cluster)
        else:
            cluster = cluster_list[s]
        #### select & cluster images as training set of this epochs
        print('Clustering and labeling...')
        if args.no_rerank:
            #euclidean_dist = -1.0 * euclidean_dist #for similarity matrix
            labels = cluster.fit_predict(e_dist[s])
        else:
            #rerank_dist = -1.0 * rerank_dist  #for similarity matrix
            labels = cluster.fit_predict(r_dist[s])
        num_ids = len(set(labels)) - 1  ##for DBSCAN cluster
        #num_ids = len(set(labels)) ##for affinity_propagation cluster
        print('Iteration {} have {} training ids'.format(n_iter+1, num_ids))
        labels_list.append(labels)
        del labels
        del cluster
    return labels_list, cluster_list

# Unsupervised, soft multilabel, MAR, eq(8)
class JointLoss(torch.nn.Module):
    def __init__(self, margin=1):
        super(JointLoss, self).__init__()
        self.margin = margin
        self.sim_margin = 1 - margin / 2

    def forward(self, features, agents, labels, similarity, features_target, similarity_target):
        """
        :param features: shape=(BS/2, dim)
        :param agents: shape=(n_class, dim)
        :param labels: shape=(BS/2,)
        :param features_target: shape=(BS/2, n_class)
        :return:
        """
        loss_terms = []
        arange = torch.arange(len(agents)).cuda()
        zero = torch.Tensor([0]).cuda()
        # ep(8), 2nd term
        for (f, l, s) in zip(features, labels, similarity):
            # src pos
            loss_pos = (f - agents[l]).pow(2).sum()
            loss_terms.append(loss_pos)
            # find hard src neg, not in paper
            neg_idx = arange != l
            hard_agent_idx = neg_idx & (s > self.sim_margin) 
            if torch.any(hard_agent_idx):
                hard_neg_sdist = (f - agents[hard_agent_idx]).pow(2).sum(dim=1)
                # push hard src neg away
                loss_neg = torch.max(zero, self.margin - hard_neg_sdist).mean()
                loss_terms.append(loss_neg)
        # eq(8), 1st term
        for (f, s) in zip(features_target, similarity_target):
            # find agent that similar to trt which should not be allowed
            hard_agent_idx = s > self.sim_margin
            if torch.any(hard_agent_idx):
                hard_neg_sdist = (f - agents[hard_agent_idx]).pow(2).sum(dim=1)
                loss_neg = torch.max(zero, self.margin - hard_neg_sdist).mean()
                loss_terms.append(loss_neg)
        loss_total = torch.mean(torch.stack(loss_terms))
        return loss_total

# Unsupervised, soft multilabel, MAR, eq(5)
class MultilabelLoss(torch.nn.Module):
    def __init__(self, batch_size, use_std=True):
        super(MultilabelLoss, self).__init__()
        self.use_std = use_std
        self.moment = batch_size / 10000

    def init_centers(self, log_multilabels, views):
        """
        :param log_multilabels: shape=(N, n_class)
        :param views: (N,)
        :return:
        """
        univiews = torch.unique(views)
        mean_ml = []
        std_ml = []
        for v in univiews:
            ml_in_v = log_multilabels[views == v]
            mean = ml_in_v.mean(dim=0)
            std = ml_in_v.std(dim=0)
            mean_ml.append(mean)
            std_ml.append(std)
        center_mean = torch.mean(torch.stack(mean_ml), dim=0)
        center_std = torch.mean(torch.stack(std_ml), dim=0)
        self.register_buffer('center_mean', center_mean)
        self.register_buffer('center_std', center_std)

    def _update_centers(self, log_multilabels, views):
        """
        :param log_multilabels: shape=(BS, n_class)
        :param views: shape=(BS,)
        :return:
        """
        univiews = torch.unique(views)
        means = []
        stds = []
        for v in univiews:
            ml_in_v = log_multilabels[views == v]
            if len(ml_in_v) == 1:
                continue
            mean = ml_in_v.mean(dim=0)
            means.append(mean)
            if self.use_std:
                std = ml_in_v.std(dim=0)
                stds.append(std)
        new_mean = torch.mean(torch.stack(means), dim=0)
        self.center_mean = self.center_mean * (1 - self.moment) + new_mean * self.moment
        if self.use_std:
            new_std = torch.mean(torch.stack(stds), dim=0)
            self.center_std = self.center_std * (1 - self.moment) + new_std * self.moment

    def forward(self, log_multilabels, views):
        """
        :param log_multilabels: shape=(BS, n_class)
        :param views: shape=(BS,)
        :return:
        """
        self._update_centers(log_multilabels.detach(), views)

        univiews = torch.unique(views)
        loss_terms = []
        for v in univiews:
            ml_in_v = log_multilabels[views == v]
            if len(ml_in_v) == 1:
                continue
            mean = ml_in_v.mean(dim=0)
            loss_mean = (mean - self.center_mean).pow(2).sum()
            loss_terms.append(loss_mean)
            std = ml_in_v.std(dim=0)
            loss_std = (std - self.center_std).pow(2).sum()
            loss_terms.append(loss_std)
        loss_total = torch.mean(torch.stack(loss_terms))
        return loss_total

# Unsupervised, soft multilabel, MAR, eq(4)
class DiscriminativeLoss(torch.nn.Module):
    def __init__(self, mining_ratio=0.001):
        super(DiscriminativeLoss, self).__init__()
        self.mining_ratio = mining_ratio
        # self.register_buffer('n_pos_pairs', torch.Tensor([0]))
        # self.register_buffer('rate_TP', torch.Tensor([0]))
        self.moment = 0.1

    def init_threshold(self, pairwise_agreements):
        pos = int(len(pairwise_agreements) * self.mining_ratio)
        sorted_agreements = np.sort(pairwise_agreements)
        t = sorted_agreements[-pos]
        self.register_buffer('threshold', torch.Tensor([t]).cuda())

    def forward(self, features, multilabels):
        """
        :param features: shape=(BS, dim)
        :param multilabels: (BS, n_class)
        :param labels: (BS,)
        :return:
        """
        P, N = self._partition_sets(features.detach(), multilabels)
        if P is None:
            pos_exponant = torch.Tensor([1]).cuda()
            num = 0
        else:
            sdist_pos_pairs = []
            for (i, j) in zip(P[0], P[1]):
                sdist_pos_pair = (features[i] - features[j]).pow(2).sum()
                sdist_pos_pairs.append(sdist_pos_pair)
            if len(sdist_pos_pairs) == 0:
                print(P)
                pos_exponant = torch.Tensor([1]).cuda()
                num = 0
            else:
                pos_exponant = torch.exp(- torch.stack(sdist_pos_pairs)).mean()
                num = -torch.log(pos_exponant)
        if N is None:
            neg_exponant = torch.Tensor([0.5]).cuda()
        else:
            sdist_neg_pairs = []
            for (i, j) in zip(N[0], N[1]):
                sdist_neg_pair = (features[i] - features[j]).pow(2).sum()
                sdist_neg_pairs.append(sdist_neg_pair)
            if len(sdist_neg_pairs) == 0:
                print(N)
                neg_exponant = torch.Tensor([0.5]).cuda()
            else:
                neg_exponant = torch.exp(- torch.stack(sdist_neg_pairs)).mean()
        den = torch.log(pos_exponant + neg_exponant)
        loss = num + den
        return loss

    def _partition_sets(self, features, multilabels):
        """
        partition the batch into confident positive, hard negative and others
        :param features: shape=(BS, dim)
        :param multilabels: shape=(BS, n_class)
        :param labels: shape=(BS,)
        :return:
        P: positive pair set. tuple of 2 np.array i and j.
            i contains smaller indices and j larger indices in the batch.
            if P is None, no positive pair found in this batch.
        N: negative pair set. similar to P, but will never be None.
        """
        p_dist = pdist(features, p=2)
        p_agree = 1 - pdist(multilabels, p=1) / 2
        sorting_idx = torch.argsort(p_dist)
        n_similar = int(len(p_dist) * self.mining_ratio)
        similar_idx = sorting_idx[:n_similar]
        is_positive = p_agree[similar_idx] > self.threshold.item()
        pos_idx = similar_idx[is_positive]
        neg_idx = similar_idx[~is_positive]
        P = dist_idx_to_pair_idx(len(features), pos_idx.float())
        N = dist_idx_to_pair_idx(len(features), neg_idx.float())
        self._update_threshold(p_agree)
        # self._update_buffers(P, labels)
        return P, N

    def _update_threshold(self, pairwise_agreements):
        pos = int(len(pairwise_agreements) * self.mining_ratio)
        sorted_agreements = torch.sort(pairwise_agreements)[0]
        t = sorted_agreements[-pos]
        self.threshold = self.threshold * (1 - self.moment) + t * self.moment

    # def _update_buffers(self, P, labels):
    #     if P is None:
    #         self.n_pos_pairs = 0.9 * self.n_pos_pairs
    #         return 0
    #     n_pos_pairs = len(P[0])
    #     count = 0
    #     for (i, j) in zip(P[0], P[1]):
    #         count += labels[i] == labels[j]
    #     rate_TP = float(count) / n_pos_pairs
    #     self.n_pos_pairs = 0.9 * self.n_pos_pairs + 0.1 * n_pos_pairs
    #     self.rate_TP = 0.9 * self.rate_TP + 0.1 * rate_TP







class RegWeightedL1Loss(nn.Module):
  def __init__(self):
    super(RegWeightedL1Loss, self).__init__()
  
  def forward(self, output, mask, ind, target):
    pred = _tranpose_and_gather_feat(output, ind)
    mask = mask.float()
    # loss = F.l1_loss(pred * mask, target * mask, reduction='elementwise_mean')
    loss = F.l1_loss(pred * mask, target * mask, reduction='sum')
    loss = loss / (mask.sum() + 1e-4)
    return loss


if __name__ == '__main__':
    center_loss = CenterLoss(2048, 751)
    features = torch.ones(16, 2048)
    targets = torch.Tensor([0, 1, 2, 3, 2, 3, 1, 4, 5, 3, 2, 1, 0, 0, 5, 4]).long()

    c_loss = center_loss(features, targets)
    print(c_loss)



        





