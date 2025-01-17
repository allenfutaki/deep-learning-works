from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F


from src.model.module.base_module import (
    ConvModule,
    SEModule,
    HSwish,
)

BN_MOMENTUM = 0.01

class ShuffleBlock(nn.Module):
    def __init__(self, inc, ouc, ksize, stride, activation, useSE, mode, affine=True):
        super(ShuffleBlock, self).__init__()
        self.stride = stride
        pad = ksize // 2
        inc = inc // 2 if stride == 1 else inc
        midc = ouc // 2

        if mode == 'v2':
            branch_main = [
                ConvModule(inc, midc, 1, activation=activation, affine=affine),
                ConvModule(midc, midc, ksize, stride=stride, padding=pad, groups=midc, activation='linear', affine=affine),
                ConvModule(midc, ouc - inc, 1, activation=activation, affine=affine),
            ]
        elif mode == 'xception':
            assert ksize == 3
            branch_main = [
                ConvModule(inc, inc, 3, stride=stride, padding=1, groups=inc, activation='linear', affine=affine),
                ConvModule(inc, midc, 1, activation=activation, affine=affine),
                ConvModule(midc, midc, 3, stride=1, padding=1, groups=midc, activation='linear', affine=affine),
                ConvModule(midc, midc, 1, activation=activation, affine=affine),
                ConvModule(midc, midc, 3, stride=1, padding=1, groups=midc, activation='linear', affine=affine),
                ConvModule(midc, ouc - inc, 1, activation=activation, affine=affine),
            ]
        else:
            raise TypeError
        
        if activation == 'relu':
            assert useSE == False
        else:
            if useSE:
                branch_main.append(SEModule(ouc - inc))
        self.branch_main = nn.Sequential(*branch_main)

        if stride == 2:
            self.branch_proj = nn.Sequential(
                ConvModule(inc, inc, ksize, stride=stride, padding=pad, groups=inc, activation='linear', affine=affine),
                ConvModule(inc, inc, 1, activation=activation, affine=affine),
            )
        else:
            self.branch_proj = None

    def forward(self, x):
        if self.stride==1:
            x_proj, x = channel_shuffle(x)
            return torch.cat((x_proj, self.branch_main(x)), 1)
        elif self.stride==2:
            x_proj = x
            return torch.cat((self.branch_proj(x_proj), self.branch_main(x)), 1)

def channel_shuffle(x):
    batchsize, num_channels, height, width = x.data.size()
    assert (num_channels % 4 == 0)
    x = x.reshape(batchsize * num_channels // 2, 2, height * width)
    x = x.permute(1, 0, 2)
    x = x.reshape(2, -1, num_channels // 2, height, width)
    return x[0], x[1]

class HighResolutionModule(nn.Module):
    def __init__(self, num_branches, block, num_blocks, num_inchannels,
                 num_channels, fuse_method, activation, useSE, multi_scale_output=True):
        super(HighResolutionModule, self).__init__()
        self._check_branches(
            num_branches, block, num_blocks, num_inchannels, num_channels)

        self.num_inchannels = num_inchannels
        self.fuse_method = fuse_method
        self.num_branches = num_branches

        self.multi_scale_output = multi_scale_output

        self.branches = self._make_branches(
            num_branches, block, num_blocks, num_channels, activation, useSE)
        self.fuse_layers = self._make_fuse_layers(activation)
        self.relu = nn.ReLU(True) if activation == 'relu' else HSwish()

    def _check_branches(self, num_branches, block, num_blocks,
                        num_inchannels, num_channels):
        if num_branches != len(num_blocks):
            error_msg = 'NUM_BRANCHES({}) <> NUM_BLOCKS({})'.format(
                num_branches, len(num_blocks))
            print(error_msg)
            raise ValueError(error_msg)

        if num_branches != len(num_channels):
            error_msg = 'NUM_BRANCHES({}) <> NUM_CHANNELS({})'.format(
                num_branches, len(num_channels))
            print(error_msg)
            raise ValueError(error_msg)

        if num_branches != len(num_inchannels):
            error_msg = 'NUM_BRANCHES({}) <> NUM_INCHANNELS({})'.format(
                num_branches, len(num_inchannels))
            print(error_msg)
            raise ValueError(error_msg)

    def _make_one_branch(self, branch_index, block, num_blocks, num_channels, activation, useSE,
                         stride=1):
        layers = []
        layers.append(block(self.num_inchannels[branch_index], num_channels[branch_index], ksize=3, stride=stride,
                        activation=activation, useSE=useSE, mode='v2'))

        self.num_inchannels[branch_index] = num_channels[branch_index]
        for _ in range(1, num_blocks[branch_index]):
            layers.append(block(self.num_inchannels[branch_index], num_channels[branch_index], ksize=3, stride=stride,
                            activation=activation, useSE=useSE, mode='v2'))

        return nn.Sequential(*layers)

    def _make_branches(self, num_branches, block, num_blocks, num_channels, activation, useSE):
        branches = []
        for i in range(num_branches):
            branches.append(
                self._make_one_branch(i, block, num_blocks, num_channels, activation, useSE)
            )
        return nn.ModuleList(branches)

    def _make_fuse_layers(self, activation):
        if self.num_branches == 1:
            return None

        num_branches = self.num_branches
        num_inchannels = self.num_inchannels
        fuse_layers = []
        for i in range(num_branches if self.multi_scale_output else 1):
            fuse_layer = []
            for j in range(num_branches):
                if j > i:
                    fuse_layer.append(
                        nn.Sequential(
                            ConvModule(num_inchannels[j], num_inchannels[i], kernel_size=1, activation='linear'),
                            nn.Upsample(scale_factor=2**(j-i), mode='nearest')
                        )
                    )
                elif j == i:
                    fuse_layer.append(None)
                else:
                    conv3x3s = []
                    for k in range(i-j):
                        if k == i - j - 1:
                            num_outchannels_conv3x3 = num_inchannels[i]
                            conv3x3s.append(
                                ConvModule(num_inchannels[j], num_outchannels_conv3x3, kernel_size=3, stride=2, padding=1, activation='linear')
                            )
                        else:
                            num_outchannels_conv3x3 = num_inchannels[j]
                            conv3x3s.append(
                                ConvModule(num_inchannels[j], num_outchannels_conv3x3, kernel_size=3, stride=2, padding=1, activation=activation)
                            )
                    fuse_layer.append(nn.Sequential(*conv3x3s))
            fuse_layers.append(nn.ModuleList(fuse_layer))

        return nn.ModuleList(fuse_layers)

    def get_num_inchannels(self):
        return self.num_inchannels

    def forward(self, x):
        if self.num_branches == 1:
            return [self.branches[0](x[0])]

        for i in range(self.num_branches):
            x[i] = self.branches[i](x[i])

        x_fuse = []

        for i in range(len(self.fuse_layers)):
            y = x[0] if i == 0 else self.fuse_layers[i][0](x[0])
            for j in range(1, self.num_branches):
                if i == j:
                    y = y + x[j]
                else:
                    y = y + self.fuse_layers[i][j](x[j])
            x_fuse.append(self.relu(y))

        return x_fuse

class PoseHighResolutionNet(nn.Module):

    def __init__(self, 
        stage_num_modules=[1,4,3],
        stage_num_branches=[2,3,4],
        stage_num_blocks=[[4,4], [4,4,4], [4,4,4,4]],
        stage_num_channels=[[24,112], [24,112,232], [24,112,232,464]],
        stage_blocks=[ShuffleBlock, ShuffleBlock, ShuffleBlock],
        stage_fused_method=['sum', 'sum', 'sum'],
        stage_activation=['relu', 'hs', 'hs'],
        stage_useSE=[False, False, True],
        classification=False,
        cifar10=False,
        ):
        super(PoseHighResolutionNet, self).__init__()

        self.inplanes = 64
        self.stage_num_branches = stage_num_branches
        self.classification = classification
        self.cifar10 = cifar10

        # stem net
        if self.cifar10:
            self.conv1 = ConvModule(3, 64, kernel_size=3, stride=1, padding=1, activation='linear')
            self.conv2 = ConvModule(64, 64, kernel_size=3, stride=1, padding=1)
        else:
            self.conv1 = ConvModule(3, 64, kernel_size=3, stride=2, padding=1, activation='linear')
            self.conv2 = ConvModule(64, 64, kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(ShuffleBlock, self.inplanes, 4)

        self.stages = nn.ModuleList()
        self.transitions = nn.ModuleList()
        self.csp_transitions = nn.ModuleList()
        self.before_branches = nn.ModuleList()
        pre_stage_channels = [self.inplanes]
        for i in range(3):
            transition = self._make_transition_layer(pre_stage_channels, stage_num_channels[i], stage_activation[max(i-1, 0)])
            csp_channels = [c//2 for c in stage_num_channels[i]]
            csp_transition = self._make_transition_layer(stage_num_channels[i], csp_channels, 'linear', csp=True)
            stage, before_branches, pre_stage_channels = self._make_stage(
                stage_num_modules[i],
                stage_num_branches[i],
                stage_num_blocks[i],
                csp_channels,
                stage_blocks[i],
                stage_fused_method[i],
                csp_channels,
                stage_activation[i],
                stage_useSE[i],
            )
            pre_stage_channels = [c*2 for c in pre_stage_channels]
            self.transitions.append(transition)
            self.stages.append(stage)
            self.csp_transitions.append(csp_transition)
            self.before_branches.append(before_branches)

        if self.classification:
            self.incre_modules, self.downsamp_modules, \
            self.final_layer = self._make_head(stage_num_channels[-1], pre_stage_channels)
        else:
            last_inp_channels = np.int(np.sum(pre_stage_channels))
            self.last_layer = ConvModule(last_inp_channels, 64, 1, activation='hs')

        self.init_weights()

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                # nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                nn.init.normal_(m.weight, std=0.001)
                for name, _ in m.named_parameters():
                    if name in ['bias']:
                        nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.ConvTranspose2d):
                nn.init.normal_(m.weight, std=0.001)
                for name, _ in m.named_parameters():
                    if name in ['bias']:
                        nn.init.constant_(m.bias, 0)

    def _make_transition_layer(
            self, num_channels_pre_layer, num_channels_cur_layer, activation, csp=False):
        num_branches_cur = len(num_channels_cur_layer)
        num_branches_pre = len(num_channels_pre_layer)

        transition_layers = nn.ModuleList()
        for i in range(num_branches_cur):
            if i < num_branches_pre:
                if num_channels_cur_layer[i] != num_channels_pre_layer[i] or csp:
                    if csp:
                        transition_layers.append(
                            ConvModule(num_channels_pre_layer[i], num_channels_cur_layer[i], kernel_size=1, activation=activation)
                        )
                        transition_layers.append(
                            ConvModule(num_channels_cur_layer[i] * 2, num_channels_cur_layer[i] * 2, kernel_size=1, activation=activation)
                        )
                    else:
                        transition_layers.append(
                            ConvModule(num_channels_pre_layer[i], num_channels_cur_layer[i], kernel_size=3, padding=1, activation=activation)
                        )
                else:
                    transition_layers.append(None)
            else:
                if not csp:
                    conv3x3s = []
                    for j in range(i+1-num_branches_pre):
                        inchannels = num_channels_pre_layer[-1]
                        outchannels = num_channels_cur_layer[i] \
                            if j == i-num_branches_pre else inchannels
                        conv3x3s.append(
                            ConvModule(inchannels, outchannels, kernel_size=3, stride=2, padding=1, activation=activation)
                        )
                    transition_layers.append(nn.Sequential(*conv3x3s))

        return transition_layers

    def _make_layer(self, block, planes, num_blocks, stride=1):
        layers = []
        layers.append(block(self.inplanes, planes, ksize=3, stride=stride,
                        activation='relu', useSE=False, mode='v2'))
        self.inplanes = planes
        for _ in range(1, num_blocks):
            layers.append(block(planes, planes, ksize=3, stride=stride,
                            activation='relu', useSE=False, mode='v2'))

        return nn.Sequential(*layers)

    def _make_stage(self, 
        num_modules,
        num_branches,
        num_blocks,
        num_channels,
        block,
        fused_method,
        num_inchannels,
        activation,
        useSE,
        multi_scale_output=True):

        modules = []
        before_branches = nn.ModuleList()
        for i in range(num_branches):
            before_branches.append(ConvModule(num_inchannels[i]*2, num_inchannels[i], kernel_size=1))

        for i in range(num_modules):
            # multi_scale_output is only used last module
            if not multi_scale_output and i == num_modules - 1:
                reset_multi_scale_output = False
            else:
                reset_multi_scale_output = True

            modules.append(
                HighResolutionModule(
                    num_branches,
                    block,
                    num_blocks,
                    num_inchannels,
                    num_channels,
                    fused_method,
                    activation,
                    useSE,
                    reset_multi_scale_output
                )
            )
            num_inchannels = modules[-1].get_num_inchannels()

        return nn.Sequential(*modules), before_branches, num_inchannels

    def _make_head(self, head_channels, pre_stage_channels):
        head_block = ShuffleBlock

        # Increasing the #channels on each resolution 
        # from C, 2C, 4C, 8C to 128, 256, 512, 1024
        incre_modules = nn.ModuleList()
        for i, channels  in enumerate(pre_stage_channels):
            self.inplanes = channels
            incre_module = self._make_layer(head_block,
                                            head_channels[i],
                                            1,
                                            stride=1)
            incre_modules.append(incre_module)
            
        # downsampling modules
        downsamp_modules = nn.ModuleList()
        for i in range(len(pre_stage_channels)-1):
            downsamp_module = ConvModule(
                in_channels=head_channels[i],
                out_channels=head_channels[i+1],
                kernel_size=3,
                stride=2,
                padding=1,
            )
            downsamp_modules.append(downsamp_module)
        feat_size = 2048
        if self.cifar10:
            feat_size = 1024
        final_layer = ConvModule(
            in_channels=head_channels[3],
            out_channels=feat_size,
            kernel_size=1
        )
        
        return incre_modules, downsamp_modules, final_layer
    
    def forward(self, x):
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.layer1(x)

        for i in range(3):
            x_list = [] 
            for j in range(self.stage_num_branches[i]):
                if self.transitions[i][j] is not None:
                    if i == 0:
                        x_list.append(self.transitions[i][j](x))
                    else:
                        if j < self.stage_num_branches[i-1]:
                            x_list.append(self.transitions[i][j](y_list[j]))
                        else:
                            x_list.append(self.transitions[i][j](y_list[-1]))
                else:
                    if i == 0:
                        x_list.append(x)
                    else:
                        x_list.append(y_list[j])

            csp_y_list = []
            for j, xs in enumerate(x_list):
                csp_y_list.append(self.csp_transitions[i][2*j](xs))
                x_list[j] = self.before_branches[i][j](xs)

            y_list = self.stages[i](x_list)
            for j, (csp_ys, ys) in enumerate(zip(csp_y_list, y_list)):
                ys = torch.cat([csp_ys, ys], axis=1)
                ys = self.csp_transitions[i][2*j+1](ys)
                y_list[j] = ys

        if self.classification:
            y = self.incre_modules[0](y_list[0])
            for i in range(len(self.downsamp_modules)):
                y = self.incre_modules[i+1](y_list[i+1]) + \
                            self.downsamp_modules[i](y)
            x = self.final_layer(y)
        else:          
            x = y_list
            # Upsampling
            x0_h, x0_w = x[0].size(2), x[0].size(3)
            x1 = F.interpolate(x[1], size=(x0_h, x0_w), mode='bilinear', align_corners=True)
            x2 = F.interpolate(x[2], size=(x0_h, x0_w), mode='bilinear', align_corners=True)
            x3 = F.interpolate(x[3], size=(x0_h, x0_w), mode='bilinear', align_corners=True)

            x = torch.cat([x[0], x1, x2, x3], 1)

            x = self.last_layer(x)

        return x

def hrnet():
    model = PoseHighResolutionNet()
    return model

def hrnet_classification():
    model = PoseHighResolutionNet(classification=True)
    return model

def hrnet_cifar():
    model = PoseHighResolutionNet(
        stage_num_modules=[1,2,1],
        stage_num_branches=[2,3,4],
        stage_num_blocks=[[2,2], [2,2,2], [2,2,2,2]],
        stage_num_channels=[[24,112], [24,112,232], [24,112,232,464]],
        stage_blocks=[ShuffleBlock, ShuffleBlock, ShuffleBlock],
        stage_activation=['relu', 'relu', 'relu'],
        stage_useSE=[False, False, False],
        classification=True,
        cifar10=True
    )
    return model

if __name__ == "__main__":
    model = PoseHighResolutionNet()
    num_params = 0.0
    for params in model.parameters():
        num_params += params.numel()
    print("Trainable parameters: {:.2f}M".format(num_params / 1000000.0))


    x = torch.rand(2, 3, 256, 192)
    out = model(x)