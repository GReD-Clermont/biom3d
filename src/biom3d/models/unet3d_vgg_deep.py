
import torch
from torch import nn
import numpy as np

from biom3d.models.encoder_vgg import EncoderBlock, VGGEncoder
from biom3d.models.decoder_vgg_deep import VGGDecoder

#---------------------------------------------------------------------------
# 3D UNet with the previous encoder and decoder

class UNet(nn.Module):
    """
    A 3D UNet architecture utilizing VGG-style encoder and decoder blocks for volumetric (3D) image segmentation.
    
    The UNet model is a convolutional neural network for fast and precise segmentation of images. 
    This implementation incorporates VGG blocks for encoding and decoding, allowing for deep feature extraction
    and reconstruction, respectively. The model supports dynamic adjustment of pooling layers and class numbers,
    along with optional deep decoder usage and weight initialization from pre-trained checkpoints.
    
    Parameters
    ----------
    num_pools : list of int
        A list of integers defining the number of pooling layers for each dimension of the input. Default is [5,5,5].
    num_classes : int
        The number of classes for segmentation. Default is 1.
    factor : int
        The scaling factor for the number of channels in VGG blocks. Default is 32.
    encoder_ckpt : str, optional
        Path to a checkpoint file from which to load encoder weights.
    model_ckpt : str, optional
        Path to a checkpoint file from which to load the entire model's weights.
    use_deep : bool
        Flag to indicate whether to use a deep decoder. Default is True.
    in_planes : int
        The number of input channels. Default is 1.
    flip_strides : bool
        Flag to flip strides to match encoder and decoder dimensions. Useful for ensuring dimensionality alignment.
    
    Attributes
    ----------
    encoder : VGGEncoder
        The encoder part of the UNet, responsible for downscaling and feature extraction.
    decoder : VGGDecoder
        The decoder part of the UNet, responsible for upscaling and constructing the segmentation map.
    
    Methods
    -------
    freeze_encoder(freeze=True)
        Freezes or unfreezes the encoder's weights.
    unfreeze_encoder()
        Convenience method to unfreeze the encoder's weights.
    load(model_ckpt)
        Loads the model's weights from a specified checkpoint.
    forward(x)
        Defines the computation performed at every call. Applies the encoder and decoder on the input.
        
    """
    def __init__(
        self, 
        num_pools=[5,5,5], 
        num_classes=1, 
        factor=32,
        encoder_ckpt = None,
        model_ckpt = None,
        use_deep=True,
        in_planes = 1,
        flip_strides = False,
        roll_strides = True, #used for models trained before commit f2ac9ee (August 2023)
        ):
        super(UNet, self).__init__()
        self.encoder = VGGEncoder(
            EncoderBlock,
            num_pools=num_pools,
            factor=factor,
            in_planes=in_planes,
            flip_strides=flip_strides,
            roll_strides=roll_strides,
            )
        self.decoder = VGGDecoder(
            EncoderBlock,
            num_pools=num_pools,
            num_classes=num_classes,
            factor_e=factor,
            factor_d=factor,
            use_deep=use_deep,
            flip_strides=flip_strides,
            roll_strides=roll_strides,
            )

        # load encoder if needed
        if encoder_ckpt is not None:
            print("Load encoder weights from", encoder_ckpt)
            if torch.cuda.is_available():
                self.encoder.cuda()
            ckpt = torch.load(encoder_ckpt)
            if 'model' in ckpt.keys():
                # remove `module.` prefix
                state_dict = {k.replace("module.", ""): v for k, v in ckpt['model'].items()} 
                # remove `0.` prefix induced by the sequential wrapper
                state_dict = {k.replace("0.layers", "layers"): v for k, v in state_dict.items()} 
                # remove `backbone.` prefix induced by pretraining 
                state_dict = {k.replace("backbone.", ""): v for k, v in state_dict.items()}
                print(self.encoder.load_state_dict(state_dict, strict=False))
            elif 'teacher' in ckpt.keys():
                # remove `module.` prefix
                state_dict = {k.replace("module.", ""): v for k, v in ckpt['teacher'].items()}  
                # remove `backbone.` prefix induced by multicrop wrapper
                state_dict = {k.replace("backbone.", ""): v for k, v in state_dict.items()}
                print(self.encoder.load_state_dict(state_dict, strict=False))
            else:
                print("[Warning] the following encoder couldn't be loaded, wrong key:", encoder_ckpt)
        
        if model_ckpt is not None:
            self.load(model_ckpt)

    def freeze_encoder(self, freeze=True):
        """
        Freezes or unfreezes the encoder's weights based on the input flag.
        
        Parameters
        ----------
        freeze : bool, optional
            If True, the encoder's weights are frozen, otherwise they are unfrozen. Default is True.
        """
        if freeze:
            print("Freezing encoder weights...")
        else:
            print("Unfreezing encoder weights...")
        for l in self.encoder.parameters(): 
            l.requires_grad = not freeze
    
    def unfreeze_encoder(self):
        """
        Unfreezes the encoder's weights. Convenience method calling `freeze_encoder` with `False`.
        """
        self.freeze_encoder(False)

    def load(self, model_ckpt):
        """Load the model from checkpoint.
        The checkpoint dictionary must have a 'model' key with the saved model for value.
        Parameters
        ----------
        model_ckpt : str
            The path to the checkpoint file containing the model's weights.
        """
        print("Load model weights from", model_ckpt)
        if torch.cuda.is_available():
            self.cuda()
            device = torch.device('cuda')
        else:
            self.cpu()
            device = torch.device('cpu')
        ckpt = torch.load(model_ckpt, map_location=device)
        if 'encoder.last_layer.weight' in ckpt['model'].keys():
            del ckpt['model']['encoder.last_layer.weight']
        print(self.load_state_dict(ckpt['model'], strict=False))

    def forward(self, x): 
        # x is an image
        out = self.encoder(x)
        out = self.decoder(out)
        return out

#---------------------------------------------------------------------------