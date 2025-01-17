from src.graph import *
import math

class _Model(nn.Module):
    def __init__(self, cfg):
        super(_Model, self).__init__()
        w, h = cfg.INPUT.SIZE
        self.out_sizes = [(w // s, h // s) for s in cfg.MODEL.STRIDES]
        self.backbone = BackboneFactory.produce(cfg)
        self.csp_head = CSPHead(self.backbone.stage_out_channels[1:], [1,2,4,8])
        self.heads = nn.ModuleDict({
            'hm': nn.ModuleList([get_ConvModule(256, 1, 1, activation='linear', use_bn=False) for _ in range(len(self.out_sizes))]),
            'wh': nn.ModuleList([get_ConvModule(256, 2, 1, activation='linear', use_bn=False) for _ in range(len(self.out_sizes))]),
            'reg': nn.ModuleList([get_ConvModule(256, 2, 1, activation='linear', use_bn=False) for _ in range(len(self.out_sizes))]),
        })
        for head in self.heads['hm']:
            for m in head.modules():
                if isinstance(m, nn.Conv2d):
                    if m.bias is not None:
                        m.bias.data.fill_(-2.19)

    def forward(self, x):
        xs = self.backbone(x)
        x = self.csp_head(xs)
        head_out = {}
        for i, out_size in enumerate(self.out_sizes):
            head_out[out_size] = {}
            for head in self.heads:
                head_out[out_size][head] = self.heads[head][i](x)
        return head_out

class _LossHead(nn.Module):
    def __init__(self, cfg):
        super(_LossHead, self).__init__()
        self.crit = {}
        self.crit['hm'] = FocalLoss()  
        self.crit['wh'] = L1Loss()
        self.crit['reg'] = L1Loss()
        self.s_hm = nn.Parameter(-1.85 * torch.ones(1))
        self.s_wh = nn.Parameter(-1.85 * torch.ones(1))
        self.s_reg = nn.Parameter(-1.85 * torch.ones(1))

    def forward(self, feats, batch):
        hm_loss = []
        wh_loss = []
        reg_loss = []
        for out_size in feats:
            for head in feats[out_size]:
                output = feats[out_size][head]
                if head == 'hm':
                    hm_loss.append(self.crit[head](output, batch[out_size]['hm']).unsqueeze(0))
                elif head == 'wh':
                    wh_loss.append(self.crit[head](output, batch[out_size]['reg_mask'], batch[out_size]['ind'], batch[out_size]['wh']).unsqueeze(0))
                elif head == 'reg':
                    reg_loss.append(self.crit[head](output, batch[out_size]['reg_mask'], batch[out_size]['ind'], batch[out_size]['reg']).unsqueeze(0))
                else:
                    raise TypeError
        losses = {'hm':torch.cat(hm_loss).mean(), 'wh':torch.cat(wh_loss).mean(), 'reg':torch.cat(reg_loss).mean()}
        loss = torch.exp(-self.s_hm) * losses['hm'] + torch.exp(-self.s_wh) * losses['wh'] + torch.exp(-self.s_reg) * losses['reg'] + self.s_hm + self.s_wh + self.s_reg
        uncertainty = {'s_hm': self.s_hm, 's_wh': self.s_wh, 's_reg': self.s_reg}
        losses.update(uncertainty)
        return loss, losses

class ShuffleNetv2CSP(BaseGraph):
    def __init__(self, cfg):
        super(ShuffleNetv2CSP, self).__init__(cfg)        
    
    def build(self):
        self.model = _Model(self.cfg)     
        self.loss_head = _LossHead(self.cfg)
        self.sub_models['loss'] = self.loss_head
            
