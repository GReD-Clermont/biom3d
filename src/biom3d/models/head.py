#---------------------------------------------------------------------------
# Dino from: https://github.com/facebookresearch/dino
#---------------------------------------------------------------------------

import torch 
from torch import nn
import math
import warnings
import numpy as np

from biom3d.models.encoder_efficientnet3d import EfficientNet3D
from biom3d.models.hrnet_2 import HighResolutionNet
from biom3d.models.encoder_vgg import VGGEncoder, EncoderBlock
from biom3d.utils import convert_num_pools

#---------------------------------------------------------------------------
# dino head from: https://github.com/facebookresearch/dino

def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor


def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)

class DINOHead(nn.Module):
    def __init__(self, in_dim, out_dim, use_bn=False, norm_last_layer=True, nlayers=3, hidden_dim=2048, bottleneck_dim=256):
        super().__init__()
        nlayers = max(nlayers, 1)
        if nlayers == 1:
            self.mlp = nn.Linear(in_dim, bottleneck_dim)
        else:
            layers = [nn.Linear(in_dim, hidden_dim)]
            if use_bn:
                layers.append(nn.BatchNorm1d(hidden_dim))
            layers.append(nn.GELU())
            for _ in range(nlayers - 2):
                layers.append(nn.Linear(hidden_dim, hidden_dim))
                if use_bn:
                    layers.append(nn.BatchNorm1d(hidden_dim))
                layers.append(nn.GELU())
            layers.append(nn.Linear(hidden_dim, bottleneck_dim))
            self.mlp = nn.Sequential(*layers)
        self.apply(self._init_weights)
        self.last_layer = nn.utils.weight_norm(nn.Linear(bottleneck_dim, out_dim, bias=False))
        self.last_layer.weight_g.data.fill_(1)
        if norm_last_layer:
            self.last_layer.weight_g.requires_grad = False

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)

    def forward(self, x, use_last=True): # use_last=False --> no normed weigth layer in the end
        if len(x.shape) != 2:
            x = x.view(x.size(0), -1)
        x = self.mlp(x)
        x = nn.functional.normalize(x, dim=-1, p=2)
        if use_last:
            x = self.last_layer(x)
        # x = nn.functional.normalize(x, dim=-1, p=2)
        return x


class MLPHead(nn.Module):
    def __init__(self, in_dim, out_dim, use_bn=False, norm_last_layer=True, nlayers=3, hidden_dim=2048, bottleneck_dim=256):
        super().__init__()
        nlayers = max(nlayers, 1)
        if nlayers == 1:
            self.mlp = nn.Linear(in_dim, bottleneck_dim)
        else:
            layers = [nn.Linear(in_dim, hidden_dim)]
            if use_bn:
                layers.append(nn.BatchNorm1d(hidden_dim))
            layers.append(nn.GELU())
            for _ in range(nlayers - 2):
                layers.append(nn.Linear(hidden_dim, hidden_dim))
                if use_bn:
                    layers.append(nn.BatchNorm1d(hidden_dim))
                layers.append(nn.GELU())
            layers.append(nn.Linear(hidden_dim, bottleneck_dim))
            self.mlp = nn.Sequential(*layers)
        self.apply(self._init_weights)

        self.bottleneck_dim = bottleneck_dim
        self.last_layer = nn.utils.weight_norm(nn.Linear(bottleneck_dim, out_dim, bias=False))

        self.last_layer.weight_g.data.fill_(1)
        self.norm_last_layer = norm_last_layer
        if norm_last_layer:
            self.last_layer.weight_g.requires_grad = False
        
        self.num_classes = out_dim

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)

    def set_num_classes(self, num_classes):
        """
        edit the number of classes, reset the loss trainable weights.
        """
        self.num_classes = num_classes

        # re-init the linear weight
        if not self.norm_last_layer:
            self.last_layer.weight_g.data.fill_(1)
        trunc_normal_(self.last_layer.weight_v, std=.02)

    def forward(self, x, use_last=True): # use_last=False --> no normed weigth layer in the end
        if len(x.shape) != 2:
            x = x.view(x.size(0), -1)
        x = self.mlp(x)
        x = nn.functional.normalize(x, dim=-1, p=2)
        if use_last:
            x = self.last_layer(x)[:,:self.num_classes]
        # x = nn.functional.normalize(x, dim=-1, p=2)
        return x

#---------------------------------------------------------------------------
# multi-crop wrapper from: https://github.com/facebookresearch/dino

class MultiCropWrapper(nn.Module):
    """
    Perform forward pass separately on each resolution input.
    The inputs corresponding to a single resolution are clubbed and single
    forward is run on the same resolution inputs. Hence we do several
    forward passes = number of different resolutions used. We then
    concatenate all the output features and run the head forward on these
    concatenated features.
    """
    def __init__(self, backbone, head):
        super(MultiCropWrapper, self).__init__()
        # disable layers dedicated to ImageNet labels classification
        backbone.fc, backbone.head = nn.Identity(), nn.Identity()
        self.backbone = backbone
        self.head = head

    def forward(self, x):
        # convert to list
        if not isinstance(x, list):
            x = [x]
        idx_crops = torch.cumsum(torch.unique_consecutive(
            torch.tensor([inp.shape[-1] for inp in x]),
            return_counts=True,
        )[1], 0)
        start_idx, output = 0, torch.empty(0).to(x[0].device)
        for end_idx in idx_crops:
            _out = self.backbone(torch.cat(x[start_idx: end_idx]))
            # The output is a tuple with XCiT model. See:
            # https://github.com/facebookresearch/xcit/blob/master/xcit.py#L404-L405
            if isinstance(_out, tuple):
                _out = _out[0]
            # accumulate outputs
            output = torch.cat((output, _out))
            start_idx = end_idx
        # Run the head forward on the concatenated features.
        return self.head(output)

#---------------------------------------------------------------------------

def dino(
    num_pools,
    emb_dim,
    out_dim=65536,
    factor=32,
    ):
    net = MultiCropWrapper(
        VGGEncoder( 
            EncoderBlock, 
            num_pools=num_pools, 
            factor = factor,
            use_emb=True,
            emb_dim=emb_dim,
        ),
        DINOHead(
            in_dim=emb_dim,
            out_dim=out_dim,
            nlayers=1,
            bottleneck_dim=128,
            norm_last_layer=True,
    ))
    return net

def vgg_mlp(
    num_pools,
    emb_dim,
    out_dim=65536,
    factor=32,
    in_planes=1,
    ):
    net = nn.Sequential(
        VGGEncoder( 
            EncoderBlock, 
            num_pools=num_pools, 
            factor = factor,
            use_emb=True,
            emb_dim=emb_dim,
            use_head=False,
            in_planes=in_planes,
        ),
        DINOHead(
            in_dim=emb_dim * 5 * 5 * 5,
            out_dim=out_dim,
            nlayers=3,
            bottleneck_dim=256,
            norm_last_layer=False,
            hidden_dim=2048,
    ))
    return net

def eff_mlp(
    emb_dim,
    out_dim=65536,
    in_planes=1,
    ):
    net = nn.Sequential(
        EfficientNet3D.from_name("efficientnet-b4", override_params={'num_classes': emb_dim}, in_channels=in_planes),
        DINOHead(
            in_dim=emb_dim,
            out_dim=out_dim,
            nlayers=3,
            bottleneck_dim=256,
            norm_last_layer=False,
            hidden_dim=2048,
    ))
    return net

def hrnet_mlp(
    patch_size,
    num_pools=[5,5,5], 
    num_classes=1, 
    factor=32,
    in_planes = 1,
    ):
    strides = np.array(convert_num_pools(num_pools=num_pools))
    strides = (strides[:2]).prod(axis=0)
    in_dim = (np.array(patch_size)/strides).prod().astype(int)
    net = nn.Sequential(
        HighResolutionNet(
            patch_size,
            num_pools=num_pools, 
            num_classes=num_classes, 
            factor=factor,
            in_planes = in_planes,
            feats_only = True,
        ),
        DINOHead(
            in_dim=in_dim,
            out_dim=2048,
            nlayers=3,
            bottleneck_dim=256,
            norm_last_layer=False,
            hidden_dim=2048,
        )
    )
    return net

#---------------------------------------------------------------------------