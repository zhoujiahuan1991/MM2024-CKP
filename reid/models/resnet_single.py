import torch
import torch.nn as nn
from .backbones.resnet import ResNet, Bottleneck
import copy
import math
# from reid.models.gem_pool import GeneralizedMeanPoolingP
from torch.nn import functional as F
class GeneralizedMeanPooling(nn.Module):
    r"""Applies a 2D power-average adaptive pooling over an input signal composed of several input planes.
    The function computed is: :math:`f(X) = pow(sum(pow(X, p)), 1/p)`
        - At p = infinity, one gets Max Pooling
        - At p = 1, one gets Average Pooling
    The output is of size H x W, for any input size.
    The number of output features is equal to the number of input planes.
    Args:
        output_size: the target output size of the image of the form H x W.
                     Can be a tuple (H, W) or a single H for a square image H x H
                     H and W can be either a ``int``, or ``None`` which means the size will
                     be the same as that of the input.
    """

    def __init__(self, norm, output_size=1, eps=1e-6):
        super(GeneralizedMeanPooling, self).__init__()
        assert norm > 0
        self.p = float(norm)
        self.output_size = output_size
        self.eps = eps

    def forward(self, x):
        x = x.clamp(min=self.eps).pow(self.p)
        return torch.nn.functional.adaptive_avg_pool2d(x, self.output_size).pow(1. / self.p)

    def __repr__(self):
        return self.__class__.__name__ + '(' \
               + str(self.p) + ', ' \
               + 'output_size=' + str(self.output_size) + ')'


class GeneralizedMeanPoolingP(GeneralizedMeanPooling):
    """ Same, but norm is trainable
    """

    def __init__(self, norm=3, output_size=1, eps=1e-6):
        super(GeneralizedMeanPoolingP, self).__init__(norm, output_size, eps)
        self.p = nn.Parameter(torch.ones(1) * norm)

class Backbone(nn.Module):
    def __init__(self,last_stride, bn_norm, with_ibn, with_se,block, num_classes,layers):
        super(Backbone, self).__init__()
        self.in_planes = 2048
        self.base = ResNet(last_stride=last_stride,
                            block=block,
                            layers=layers)
        print('using resnet50 as a backbone')

        
        self.bottleneck = nn.BatchNorm2d(2048)
        self.bottleneck.bias.requires_grad_(False)
        nn.init.constant_(self.bottleneck.weight, 1)
        nn.init.constant_(self.bottleneck.bias, 0)

        self.pooling_layer = GeneralizedMeanPoolingP(3)

        self.classifier = nn.Linear(512*block.expansion, num_classes, bias=False)
        nn.init.normal_(self.classifier.weight, std=0.001)
       

        self.random_init()
        self.num_classes = num_classes
    def forward(self, x, domains=None, training_phase=None, get_all_feat=False,epoch=0):        
        x = self.base(x)
        global_feat = self.pooling_layer(x) # [16, 2048, 1, 1]
        bn_feat = self.bottleneck(global_feat) # [16, 2048, 1, 1]
        
        # global_feat=F.normalize(global_feat)    # L2 normalization
            

        if get_all_feat is True:
            cls_outputs = self.classifier(bn_feat[..., 0, 0])
            return global_feat[..., 0, 0], bn_feat[..., 0, 0], cls_outputs, x

        if self.training is False:
            # return bn_feat[..., 0, 0]
            return global_feat[..., 0, 0]

        bn_feat = bn_feat[..., 0, 0]
        cls_outputs = self.classifier(bn_feat)      
        return global_feat[..., 0, 0], bn_feat, cls_outputs, x

    def load_param(self, trained_path):
        param_dict = torch.load(trained_path)
        if 'state_dict' in param_dict:
            param_dict = param_dict['state_dict']
        for i in param_dict:
            self.state_dict()[i].copy_(param_dict[i])
        print('Loading pretrained model from {}'.format(trained_path))

    def load_param_finetune(self, model_path):
        param_dict = torch.load(model_path)
        for i in param_dict:
            self.state_dict()[i].copy_(param_dict[i])
        print('Loading pretrained model for finetuning from {}'.format(model_path))

    def random_init(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                nn.init.normal_(m.weight, 0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

def make_model(arg, num_class, camera_num, view_num,pretrain=True):
    model = Backbone(1, 'BN', False, False, Bottleneck, num_class, [3, 4, 6, 3])
    print('===========building ResNet===========')
    if pretrain:
        import torchvision
        res_base = torchvision.models.resnet50(pretrained=True)
        res_base_dict = res_base.state_dict()

        state_dict = model.base.state_dict()
        for k, v in res_base_dict.items():
            if k in state_dict:
                if v.shape == state_dict[k].shape:
                    state_dict[k] = v
                else:
                    print('param {} of shape {} does not match loaded shape {}'.format(k, v.shape,
                                                                                       state_dict[k].shape))
            else:
                print('param {} in pre-trained model does not exist in this model.base'.format(k))

        model.base.load_state_dict(state_dict, strict=True)
    return model
