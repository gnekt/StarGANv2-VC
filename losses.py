#coding:utf-8

import os
import torch

from torch import nn
from munch import Munch
from transforms import build_transforms

import torch.nn.functional as F
import numpy as np

def compute_d_loss(nets, args, x_real, y_org, y_trg, x_ref=None, use_r1_reg=True, use_adv_cls=False, use_con_reg=False):
    """_summary_

    Args:
        nets (_type_): _description_
        args (_type_): _description_
        x_real (_type_): (Batch, 1, NMels, Mel_Dim)
        y_org (_type_): (Batch) Source Emotion label, in our case always Neutral
        y_trg (_type_): (Batch) Target Emotion label
        x_ref ((Batch, 1, NMels, Mel_Dim), optional): _description_. Defaults to None.
        use_r1_reg (bool, optional): _description_. Defaults to True.
        use_adv_cls (bool, optional): _description_. Defaults to False.
        use_con_reg (bool, optional): _description_. Defaults to False.
    Returns:
        _type_: _description_
    """    
    args = Munch(args)

    assert x_ref is not None
    # with real audios
    x_real.requires_grad_()
    out = nets.discriminator(x_real, y_org)
    loss_real = adv_loss(out, 1)
    
    # R1 regularizaition (https://arxiv.org/abs/1801.04406v4)
    if use_r1_reg:
        loss_reg = r1_reg(out, x_real)
    else:
        loss_reg = torch.FloatTensor([0]).to(x_real.device)
    
    # consistency regularization (bCR-GAN: https://arxiv.org/abs/2002.04724)
    loss_con_reg = torch.FloatTensor([0]).to(x_real.device)
    if use_con_reg:
        t = build_transforms()
        out_aug = nets.discriminator(t(x_real).detach(), y_org)
        loss_con_reg += F.smooth_l1_loss(out, out_aug)
    
    # with fake audios
    with torch.no_grad():
        s_trg = nets.emotion_encoder(x_ref)
        x_fake = nets.generator(x_real, s_trg, masks=None)
    out = nets.discriminator(x_fake, y_trg)
    loss_fake = adv_loss(out, 0)
    if use_con_reg:
        out_aug = nets.discriminator(t(x_fake).detach(), y_trg)
        loss_con_reg += F.smooth_l1_loss(out, out_aug)
    
    # adversarial classifier loss
    if use_adv_cls:
        out_de = nets.discriminator.classifier(x_fake)
        loss_real_adv_cls = F.cross_entropy(out_de[y_org != y_trg], y_org[y_org != y_trg])
        
        if use_con_reg:
            out_de_aug = nets.discriminator.classifier(t(x_fake).detach())
            loss_con_reg += F.smooth_l1_loss(out_de, out_de_aug)
    else:
        loss_real_adv_cls = torch.zeros(1).mean()
        
    loss = loss_real + loss_fake + args.lambda_reg * loss_reg + \
            args.lambda_adv_cls * loss_real_adv_cls + \
            args.lambda_con_reg * loss_con_reg 

    return loss, Munch(real=loss_real.item(),
                       fake=loss_fake.item(),
                       reg=loss_reg.item(),
                       real_adv_cls=loss_real_adv_cls.item(),
                       con_reg=loss_con_reg.item())

def compute_g_loss(nets, args, x_real, y_org, y_trg, x_refs=None, use_adv_cls=False):
    args = Munch(args)

    x_ref, x_ref2 = x_refs
        
    # compute style vectors
    s_trg = nets.emotion_encoder(x_ref)
    
    # compute ASR/F0 features (real)
    with torch.no_grad():
        ASR_real = nets.asr_model.get_feature(x_real)
    
    # adversarial loss
    x_fake = nets.generator(x_real, s_trg, masks=None)
    out = nets.discriminator(x_fake, y_trg) 
    loss_adv = adv_loss(out, 1)
    
    # compute ASR/F0 features (fake)
    ASR_fake = nets.asr_model.get_feature(x_fake)
    
    # norm consistency loss
    x_fake_norm = log_norm(x_fake)
    x_real_norm = log_norm(x_real)
    loss_norm = ((torch.nn.ReLU()(torch.abs(x_fake_norm - x_real_norm) - args.norm_bias))**2).mean()
    
    # ASR loss
    loss_asr = F.smooth_l1_loss(ASR_fake, ASR_real)
    
    # style reconstruction loss
    s_pred = nets.emotion_encoder(x_fake)
    loss_sty = torch.mean(torch.abs(s_pred - s_trg))
    
    # diversity sensitive loss
    s_trg2 = nets.emotion_encoder(x_ref2)
    x_fake2 = nets.generator(x_real, s_trg2, masks=None)
    x_fake2 = x_fake2.detach()
    loss_ds = torch.mean(torch.abs(x_fake - x_fake2))
    
    # cycle-consistency loss
    s_org = nets.emotion_encoder(x_real)
    x_rec = nets.generator(x_fake, s_org, masks=None)
    loss_cyc = torch.mean(torch.abs(x_rec - x_real))
    # ASR loss in cycle-consistency loss
    if args.lambda_f0 > 0:
        if args.lambda_asr > 0:
            ASR_recon = nets.asr_model.get_feature(x_rec)
            loss_cyc += F.smooth_l1_loss(ASR_recon, ASR_real)
    
    # adversarial classifier loss
    if use_adv_cls:
        out_de = nets.discriminator.classifier(x_fake)
        loss_adv_cls = F.cross_entropy(out_de[y_org != y_trg], y_trg[y_org != y_trg])
    else:
        loss_adv_cls = torch.zeros(1).mean()
    
    loss = args.lambda_adv * loss_adv + args.lambda_sty * loss_sty \
           - args.lambda_ds * loss_ds + args.lambda_cyc * loss_cyc\
           + args.lambda_norm * loss_norm \
           + args.lambda_asr * loss_asr \
           + args.lambda_adv_cls * loss_adv_cls

    return loss, Munch(adv=loss_adv.item(),
                       sty=loss_sty.item(),
                       ds=loss_ds.item(),
                       cyc=loss_cyc.item(),
                       norm=loss_norm.item(),
                       asr=loss_asr.item(),
                       adv_cls=loss_adv_cls.item())
    
# for norm consistency loss
def log_norm(x, mean=-4, std=4, dim=2):
    """
    normalized log mel -> mel -> norm -> log(norm)
    """
    x = torch.log(torch.exp(x * std + mean).norm(dim=dim))
    return x

# for adversarial loss
def adv_loss(logits, target):
    assert target in [1, 0]
    if len(logits.shape) > 1:
        logits = logits.reshape(-1)
    targets = torch.full_like(logits, fill_value=target)
    logits = logits.clamp(min=-10, max=10) # prevent nan
    loss = F.binary_cross_entropy_with_logits(logits, targets)
    return loss

# for R1 regularization loss
def r1_reg(d_out, x_in):
    # zero-centered gradient penalty for real images
    batch_size = x_in.size(0)
    grad_dout = torch.autograd.grad(
        outputs=d_out.sum(), inputs=x_in,
        create_graph=True, retain_graph=True, only_inputs=True
    )[0]
    grad_dout2 = grad_dout.pow(2)
    assert(grad_dout2.size() == x_in.size())
    reg = 0.5 * grad_dout2.view(batch_size, -1).sum(1).mean(0)
    return reg