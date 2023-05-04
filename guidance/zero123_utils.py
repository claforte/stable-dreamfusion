import math
from omegaconf import OmegaConf

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.cuda.amp import custom_bwd, custom_fwd

from diffusers import DDIMScheduler

import sys
from os import path
sys.path.append(path.dirname(path.dirname(path.abspath(__file__))))

from ldm.util import instantiate_from_config

class SpecifyGradient(torch.autograd.Function):
    @staticmethod
    @custom_fwd
    def forward(ctx, input_tensor, gt_grad):
        ctx.save_for_backward(gt_grad)
        # we return a dummy value 1, which will be scaled by amp's scaler so we get the scale in backward.
        return torch.ones([1], device=input_tensor.device, dtype=input_tensor.dtype)

    @staticmethod
    @custom_bwd
    def backward(ctx, grad_scale):
        gt_grad, = ctx.saved_tensors
        gt_grad = gt_grad * grad_scale
        return gt_grad, None

# load model
def load_model_from_config(config, ckpt, device, vram_O=False, zero123_final=False, verbose=False):

    pl_sd = torch.load(ckpt, map_location='cpu')

    if 'global_step' in pl_sd and verbose:
        print(f'[INFO] Global Step: {pl_sd["global_step"]}')

    sd = pl_sd['state_dict']

    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)

    if len(m) > 0 and verbose:
        print('[INFO] missing keys: \n', m)
    if len(u) > 0 and verbose:
        print('[INFO] unexpected keys: \n', u)

    # manually load ema and delete it to save GPU memory
    if model.use_ema:
        if verbose:
            print('[INFO] loading EMA...')
        model.model_ema.copy_to(model.model)
        del model.model_ema

    if vram_O and not zero123_final:
        # we don't need decoder
        del model.first_stage_model.decoder

    torch.cuda.empty_cache()

    model.eval().to(device)

    return model

class Zero123(nn.Module):
    def __init__(self, device, fp16,
                 config='./pretrained/zero123/sd-objaverse-finetune-c_concat-256.yaml',
                 ckpt='./pretrained/zero123/105000.ckpt', vram_O=False, t_range=[0.02, 0.98], zero123_final=False):
        super().__init__()

        # # hardcoded
        # config = './pretrained/zero123/sd-objaverse-finetune-c_concat-256.yaml'
        # ckpt = './pretrained/zero123/105000.ckpt'

        self.device = device
        self.fp16 = fp16
        self.vram_O = vram_O
        self.t_range = t_range
        self.zero123_final = zero123_final

        self.config = OmegaConf.load(config)
        # TODO: seems it cannot load into fp16...
        self.model = load_model_from_config(self.config, ckpt, device=self.device, vram_O=vram_O, zero123_final=zero123_final)

        # timesteps: use diffuser for convenience... hope it's alright.
        self.num_train_timesteps = self.config.model.params.timesteps

        self.scheduler = DDIMScheduler(
            self.num_train_timesteps,
            self.config.model.params.linear_start,
            self.config.model.params.linear_end,
            beta_schedule='scaled_linear',
            clip_sample=False,
            set_alpha_to_one=False,
            steps_offset=1,
        )

        self.min_step = int(self.num_train_timesteps * t_range[0])
        self.max_step = int(self.num_train_timesteps * t_range[1])
        self.alphas = self.scheduler.alphas_cumprod.to(self.device) # for convenience

    @torch.no_grad()
    def get_img_embeds(self, x):
        # x: image tensor [B, 3, 256, 256] in [0, 1]
        x = x * 2 - 1
        c = [self.model.get_learned_conditioning(xx.unsqueeze(0)) for xx in x] #.tile(n_samples, 1, 1)
        v = [self.model.encode_first_stage(xx.unsqueeze(0)).mode() for xx in x]
        return c, v

    def train_step(self, embeddings, pred_rgb, polar, azimuth, radius, guidance_scale=3, as_latent=False, grad_scale=1):
        # pred_rgb: tensor [1, 3, H, W] in [0, 1]

        if as_latent:
            latents = F.interpolate(pred_rgb, (32, 32), mode='bilinear', align_corners=False) * 2 - 1
        else:
            pred_rgb_256 = F.interpolate(pred_rgb, (256, 256), mode='bilinear', align_corners=False)
            latents = self.encode_imgs(pred_rgb_256)

        t = torch.randint(self.min_step, self.max_step + 1, [1], dtype=torch.long, device=self.device)

        # Set weights acc to closeness in phi
        if len(embeddings['phis']) > 1:
            phi_deltas = [abs(azimuth.item() + embeddings['phis'][0] - phi) for phi in embeddings['phis']]
            phi_deltas = [abs(p-360) if p > 180 else p for p in phi_deltas]
            inv_phi_deltas = [min(1/p, 1000) if p != 0 else 1000 for p in phi_deltas]
            inv_phi_deltas = [p/max(inv_phi_deltas) for p in inv_phi_deltas]
            inv_phi_deltas = [0 if p < 0.1 else p for p in inv_phi_deltas]
        else:
            inv_phi_deltas = [1]

        # Multiply closeness-weight by user-given weights
        img_ws = [a*b for (a, b) in zip(embeddings['img_ws'], inv_phi_deltas)]
        img_ws = [img_w/max(img_ws) for img_w in img_ws]
        img_ws = [0 if img_w < 0.1 else img_w for img_w in img_ws]

        with torch.no_grad():
            noise = torch.randn_like(latents)
            latents_noisy = self.scheduler.add_noise(latents, noise, t)

            x_in = torch.cat([latents_noisy] * 2)
            t_in = torch.cat([t] * 2)

            noise_preds = []
            for (img_w, c_crossattn, c_concat, theta, phi, rad) in zip(img_ws, embeddings['c_crossattn'], embeddings['c_concat'],
                                                                       embeddings['thetas'], embeddings['phis'], embeddings['radii']):
                if img_w == 0:
                    continue
                # polar,azimuth,radius are all actually delta wrt default
                p = polar + embeddings['thetas'][0] - theta
                a = azimuth + embeddings['phis'][0] - phi
                a[a > 180] -= 360 # range in [-180, 180]
                r = radius + embeddings['radii'][0] - rad
                T = torch.tensor([math.radians(p), math.sin(math.radians(a)), math.cos(math.radians(a)), r])
                T = T[None, None, :].to(self.device)
                cond = {}
                clip_emb = self.model.cc_projection(torch.cat([c_crossattn, T], dim=-1))
                cond['c_crossattn'] = [torch.cat([torch.zeros_like(clip_emb).to(self.device), clip_emb], dim=0)]
                cond['c_concat'] = [torch.cat([torch.zeros_like(c_concat).to(self.device), c_concat], dim=0)]
                noise_pred = self.model.apply_model(x_in, t_in, cond)
                noise_pred_uncond, noise_pred_cond = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_cond - noise_pred_uncond)
                noise_preds.append(img_w * noise_pred)

        noise_pred = torch.stack(noise_preds).sum(dim=0) / sum(img_ws)

        w = (1 - self.alphas[t])
        grad = grad_scale * w * (noise_pred - noise)
        grad = torch.nan_to_num(grad)

        # import kiui
        # if not as_latent:
        #     kiui.vis.plot_image(pred_rgb_256)
        # kiui.vis.plot_matrix(latents)
        # kiui.vis.plot_matrix(grad)

        # import kiui
        # latents = torch.randn((1, 4, 32, 32), device=self.device)
        # kiui.lo(latents)
        # self.scheduler.set_timesteps(30)
        # with torch.no_grad():
        #     for i, t in enumerate(self.scheduler.timesteps):
        #         x_in = torch.cat([latents] * 2)
        #         t_in = torch.cat([t.view(1)] * 2).to(self.device)

        #         noise_pred = self.model.apply_model(x_in, t_in, cond)
        #         noise_pred_uncond, noise_pred_cond = noise_pred.chunk(2)
        #         noise_pred = noise_pred_uncond + 3 * (noise_pred_cond - noise_pred_uncond)

        #         latents = self.scheduler.step(noise_pred, t, latents)['prev_sample']
        # imgs = self.decode_latents(latents)
        # print(polar, azimuth, radius)
        # kiui.vis.plot_image(pred_rgb_256, imgs)

        # since we omitted an item in grad, we need to use the custom function to specify the gradient
        loss = SpecifyGradient.apply(latents, grad)

        return loss

    # verification
    @torch.no_grad()
    def __call__(self,
            image, # image tensor [1, 3, H, W] in [0, 1]
            polar=0, azimuth=0, radius=0, # new view params
            scale=3, ddim_steps=50, ddim_eta=1, h=256, w=256, # diffusion params
            c_crossattn=None, c_concat=None, post_process=True,
        ):

        if c_crossattn is None:
            embeddings = self.get_img_embeds(image)
        T = torch.tensor([math.radians(polar), math.sin(math.radians(azimuth)), math.cos(math.radians(azimuth)), radius])
        T = T[None, None, :].to(self.device)

        cond = {}
        clip_emb = self.model.cc_projection(torch.cat([embeddings['c_crossattn'] if c_crossattn is None else c_crossattn, T], dim=-1))
        cond['c_crossattn'] = [torch.cat([torch.zeros_like(clip_emb).to(self.device), clip_emb], dim=0)]
        cond['c_concat'] = [torch.cat([torch.zeros_like(embeddings['c_concat']).to(self.device), embeddings['c_concat']], dim=0)] if c_concat is None else [torch.cat([torch.zeros_like(c_concat).to(self.device), c_concat], dim=0)]

        # produce latents loop
        latents = torch.randn((1, 4, h // 8, w // 8), device=self.device)
        self.scheduler.set_timesteps(ddim_steps)

        for i, t in enumerate(self.scheduler.timesteps):
            x_in = torch.cat([latents] * 2)
            t_in = torch.cat([t.view(1)] * 2).to(self.device)

            noise_pred = self.model.apply_model(x_in, t_in, cond)
            noise_pred_uncond, noise_pred_cond = noise_pred.chunk(2)
            noise_pred = noise_pred_uncond + scale * (noise_pred_cond - noise_pred_uncond)

            latents = self.scheduler.step(noise_pred, t, latents, eta=ddim_eta)['prev_sample']

        imgs = self.decode_latents(latents)
        imgs = imgs.cpu().numpy().transpose(0, 2, 3, 1) if post_process else imgs

        return imgs

    # verification
    @torch.no_grad()
    def gen_from_multiview(self,
            polar=0, azimuth=0, radius=0, # new view params
            img_ws=None, thetas=None, phis=None, radii=None,
            c_crossattns=None, c_concats=None, post_process=True,
            scale=3, ddim_steps=50, ddim_eta=1, h=256, w=256, # diffusion params
        ):

        Ts = [torch.tensor([math.radians(polar-thetas[0]+theta), math.sin(math.radians(azimuth-phis[0]+phi)), math.cos(math.radians(azimuth-phis[0]+phi)), radius-radii[0]+rad]) for (theta, phi, rad) in zip(thetas, phis, radii)]
        Ts = [T[None, None, :].to(self.device) for T in Ts]

        clip_embs = [self.model.cc_projection(torch.cat([c_crossattn, T], dim=-1)) for (c_crossattn, T) in zip(c_crossattns, Ts)]
        conds = []
        for (clip_emb, c_concat) in zip(clip_embs, c_concats):
            cond = {}
            cond['c_crossattn'] = [torch.cat([torch.zeros_like(clip_emb).to(self.device), clip_emb], dim=0)]
            cond['c_concat'] = [torch.cat([torch.zeros_like(c_concat).to(self.device), c_concat], dim=0)]
            conds.append(cond)

        # produce latents loop
        latents = torch.randn((1, 4, h // 8, w // 8), device=self.device)
        self.scheduler.set_timesteps(ddim_steps)

        for i, t in enumerate(self.scheduler.timesteps):
            x_in = torch.cat([latents] * 2)
            t_in = torch.cat([t.view(1)] * 2).to(self.device)

            noise_preds = []
            for (img_w, cond) in zip(img_ws, conds):
                if img_w == 0:
                    continue
                noise_pred = self.model.apply_model(x_in, t_in, cond)
                noise_pred_uncond, noise_pred_cond = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + scale * (noise_pred_cond - noise_pred_uncond)
                noise_preds.append(img_w * noise_pred)

            noise_pred = torch.stack(noise_preds).sum(dim=0) / sum(img_ws)

            latents = self.scheduler.step(noise_pred, t, latents, eta=ddim_eta)['prev_sample']

        imgs = self.decode_latents(latents)
        imgs = imgs.cpu().numpy().transpose(0, 2, 3, 1) if post_process else imgs

        return imgs

    def decode_latents(self, latents):
        # zs: [B, 4, 32, 32] Latent space image
        # with self.model.ema_scope():
        imgs = self.model.decode_first_stage(latents)
        imgs = (imgs / 2 + 0.5).clamp(0, 1)

        return imgs # [B, 3, 256, 256] RGB space image

    def encode_imgs(self, imgs):
        # imgs: [B, 3, 256, 256] RGB space image
        # with self.model.ema_scope():
        imgs = imgs * 2 - 1
        latents = self.model.get_first_stage_encoding(self.model.encode_first_stage(imgs))
        return latents # [B, 4, 32, 32] Latent space image


if __name__ == '__main__':
    import cv2
    import argparse
    import numpy as np
    import matplotlib.pyplot as plt

    parser = argparse.ArgumentParser()

    parser.add_argument('input', type=str)
    parser.add_argument('--fp16', action='store_true', help="use float16 for training") # no use now, can only run in fp32

    parser.add_argument('--polar', type=float, default=0, help='delta polar angle in [-90, 90]')
    parser.add_argument('--azimuth', type=float, default=0, help='delta azimuth angle in [-180, 180]')
    parser.add_argument('--radius', type=float, default=0, help='delta camera radius multiplier in [-0.5, 0.5]')

    opt = parser.parse_args()

    device = torch.device('cuda')

    print(f'[INFO] loading image from {opt.input} ...')
    image = cv2.imread(opt.input, cv2.IMREAD_UNCHANGED)
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image = cv2.resize(image, (256, 256), interpolation=cv2.INTER_AREA)
    image = image.astype(np.float32) / 255.0
    image = torch.from_numpy(image).permute(2, 0, 1).unsqueeze(0).contiguous().to(device)

    print(f'[INFO] loading model ...')
    zero123 = Zero123(device, opt.fp16)

    print(f'[INFO] running model ...')
    outputs = zero123(image, polar=opt.polar, azimuth=opt.azimuth, radius=opt.radius)
    plt.imshow(outputs[0])
    plt.show()