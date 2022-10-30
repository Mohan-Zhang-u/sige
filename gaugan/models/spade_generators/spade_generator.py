import torch
from torch import nn
from torch.nn import functional as F
from torch.nn.utils import remove_spectral_norm, spectral_norm

from ..normalization import SPADE


class SPADEResnetBlock(nn.Module):
    def __init__(self, fin, fout, opt):
        super(SPADEResnetBlock, self).__init__()
        self.fin = fin
        self.fout = fout
        self.opt = opt
        # Attributes
        self.learned_shortcut = fin != fout
        fmiddle = min(fin, fout)

        # create conv layers
        self.conv_0 = nn.Conv2d(fin, fmiddle, kernel_size=3, padding=1)
        self.conv_1 = nn.Conv2d(fmiddle, fout, kernel_size=3, padding=1)
        if self.learned_shortcut:
            self.conv_s = nn.Conv2d(fin, fout, kernel_size=1, bias=False)
        if "spectral" in opt.norm_G:
            self.conv_0 = spectral_norm(self.conv_0)
            self.conv_1 = spectral_norm(self.conv_1)
            if self.learned_shortcut:
                self.conv_s = spectral_norm(self.conv_s)

        # define normalization layers
        spade_config_str = opt.norm_G.replace("spectral", "")
        self.norm_0 = SPADE(spade_config_str, fin, opt.semantic_nc, nhidden=opt.ngf * 2)
        self.norm_1 = SPADE(spade_config_str, fmiddle, opt.semantic_nc, nhidden=opt.ngf * 2)
        if self.learned_shortcut:
            self.norm_s = SPADE(spade_config_str, fin, opt.semantic_nc, nhidden=opt.ngf * 2)

    # note the resnet block with SPADE also takes in |seg|,
    # the semantic segmentation map as input
    def forward(self, x, seg):
        x_s = self.shortcut(x, seg)

        dx = self.conv_0(self.actvn(self.norm_0(x, seg)))
        dx = self.conv_1(self.actvn(self.norm_1(dx, seg)))

        out = x_s + dx

        return out

    def shortcut(self, x, seg):
        if self.learned_shortcut:
            x_s = self.conv_s(self.norm_s(x, seg))
        else:
            x_s = x
        return x_s

    def actvn(self, x):
        return F.leaky_relu(x, 2e-1)

    def remove_spectral_norm(self):
        self.conv_0 = remove_spectral_norm(self.conv_0)
        self.conv_1 = remove_spectral_norm(self.conv_1)
        if self.learned_shortcut:
            self.conv_s = remove_spectral_norm(self.conv_s)


class SPADEGenerator(nn.Module):
    def __init__(self, opt, **kwargs):
        super(SPADEGenerator, self).__init__()
        self.opt = opt
        nf = opt.ngf

        self.sw, self.sh = self.compute_latent_vector_size(opt)

        self.fc = nn.Conv2d(self.opt.semantic_nc, 16 * nf, 3, padding=1)

        self.head_0 = SPADEResnetBlock(16 * nf, 16 * nf, opt)

        self.G_middle_0 = SPADEResnetBlock(16 * nf, 16 * nf, opt)
        self.G_middle_1 = SPADEResnetBlock(16 * nf, 16 * nf, opt)

        self.up_0 = SPADEResnetBlock(16 * nf, 8 * nf, opt)
        self.up_1 = SPADEResnetBlock(8 * nf, 4 * nf, opt)
        self.up_2 = SPADEResnetBlock(4 * nf, 2 * nf, opt)
        self.up_3 = SPADEResnetBlock(2 * nf, 1 * nf, opt)

        final_nc = nf

        if opt.num_upsampling_layers == "most":
            self.up_4 = SPADEResnetBlock(1 * nf, nf // 2, opt)
            final_nc = nf // 2

        self.conv_img = nn.Conv2d(final_nc, 3, 3, padding=1)

        self.up = nn.Upsample(scale_factor=2)

    def compute_latent_vector_size(self, opt):
        if opt.num_upsampling_layers == "normal":
            num_up_layers = 5
        elif opt.num_upsampling_layers == "more":
            num_up_layers = 6
        elif opt.num_upsampling_layers == "most":
            num_up_layers = 7
        else:
            raise ValueError("opt.num_upsampling_layers [%s] not recognized" % opt.num_upsampling_layers)

        sw = opt.crop_size // (2**num_up_layers)
        sh = round(sw / opt.aspect_ratio)

        return sw, sh

    def forward(self, input):
        opt = self.opt
        seg = input

        # we downsample segmap and run convolution
        x = F.interpolate(seg, size=(self.sh, self.sw))
        x = self.fc(x)

        x = self.head_0(x, seg)

        x = self.up(x)
        x = self.G_middle_0(x, seg)

        if opt.num_upsampling_layers in ("more", "most"):
            x = self.up(x)

        x = self.G_middle_1(x, seg)

        x = self.up(x)
        x = self.up_0(x, seg)

        x = self.up(x)
        x = self.up_1(x, seg)

        x = self.up(x)
        x = self.up_2(x, seg)

        x = self.up(x)
        x = self.up_3(x, seg)

        if self.opt.num_upsampling_layers == "most":
            x = self.up(x)
            x = self.up_4(x, seg)

        x = self.conv_img(F.leaky_relu(x, 2e-1))
        x = torch.tanh(x)

        return x

    def remove_spectral_norm(self):
        self.head_0.remove_spectral_norm()
        self.G_middle_0.remove_spectral_norm()
        self.G_middle_1.remove_spectral_norm()

        self.up_0.remove_spectral_norm()
        self.up_1.remove_spectral_norm()
        self.up_2.remove_spectral_norm()
        self.up_3.remove_spectral_norm()

        if self.opt.num_upsampling_layers == "most":
            self.up_4.remove_spectral_norm()
