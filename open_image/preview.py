"""Live previews of a picture while it is being drawn.

At every step the scheduler knows (or lets us work out) its current guess of the finished picture, still in
latent form. Running the VAE decoder on that every step would cost too much on a small GPU, so the latent is
mapped straight to colour with a small linear projection. For model families with a known projection the colours
come out close to the real thing; for the rest the three strongest latent components are shown as false colour.
"""
import io
import time

import torch
from PIL import Image

# rows are latent channels, columns are R, G, B. SDXL and FLUX are least-squares fits of real pictures encoded by
# those VAEs; SD15 is the projection in common use
SDXL = ([[0.4052, 0.391, 0.4308], [-0.2301, 0.0551, 0.0172], [0.1115, 0.2149, -0.0445], [-0.328, -0.2755, -0.2185]],
        [0.0651, -0.062, 0.0499])
SD15 = ([[0.3512, 0.2297, 0.3227], [0.3250, 0.4974, 0.2350], [-0.2829, 0.1762, 0.2721], [-0.2120, -0.2616, -0.7177]],
        [0.0, 0.0, 0.0])
FLUX = ([[-0.0033, 0.0445, 0.0814], [0.0034, 0.0191, 0.084], [0.0385, -0.0619, -0.0112], [-0.0254, 0.006, 0.0423],
         [0.0634, 0.0444, -0.0014], [-0.0033, 0.0461, -0.03], [0.0, 0.0583, 0.097], [-0.0619, -0.0466, -0.086],
         [-0.0411, -0.0344, 0.1141], [0.0973, 0.093, -0.0628], [-0.013, 0.0848, 0.0448], [0.098, 0.034, 0.0268],
         [0.0392, 0.0409, 0.0676], [-0.1317, -0.0219, -0.1168], [-0.0347, -0.0955, -0.0298], [-0.1137, -0.085, -0.0574]],
        [-0.1504, -0.147, -0.0877])
FACTORS = {"sdxl": SDXL, "sdxl-anime": SDXL, "sdxl-single": SDXL, "sd15": SD15, "chroma": FLUX, "zimage": FLUX}
EVERY_S = 0.3
SIZE = 320


class Preview:
    def __init__(self, family, width, height):
        self.family = family
        self.width, self.height = width, height
        self.x0 = None
        self.basis = None
        self.last = 0.0

    def attach(self, pipe):
        """Wrap the scheduler's step so the current guess of the finished latent is kept after every step."""
        sched = pipe.scheduler
        original = getattr(sched, "_open_image_step", None) or sched.step
        preview = self

        def step(model_output, timestep, sample, *args, **kwargs):
            index = getattr(sched, "step_index", None)
            out = original(model_output, timestep, sample, *args, **kwargs)
            try:
                preview.x0 = preview._guess(sched, index, model_output, sample, out)
            except Exception:
                preview.x0 = None
            return out

        sched._open_image_step = original
        sched.step = step

    @staticmethod
    def _guess(sched, index, model_output, sample, out):
        if isinstance(out, tuple) and len(out) > 1 and torch.is_tensor(out[1]):
            return out[1].detach()
        if getattr(out, "pred_original_sample", None) is not None:
            return out.pred_original_sample.detach()
        if "FlowMatch" in type(sched).__name__ and index is not None:
            sigma = sched.sigmas[index].to(sample.device, torch.float32)
            return (sample.float() - sigma * model_output.float()).detach()
        prev = out[0] if isinstance(out, tuple) else out.prev_sample
        return prev.detach()

    def _unpack(self, tokens):
        """Packed 2x2 patch tokens (sequence, channels*4) back to (channels, height, width)."""
        seq, c4 = tokens.shape
        rows = max(1, round((seq * self.height / self.width) ** 0.5))
        cols = seq // rows
        if rows * cols != seq or c4 % 4:
            return None
        c = c4 // 4
        return tokens.view(rows, cols, c, 2, 2).permute(2, 0, 3, 1, 4).reshape(c, rows * 2, cols * 2)

    def _false_colour(self, x):
        c, h, w = x.shape
        flat = x.reshape(c, -1)
        flat = flat - flat.mean(dim=1, keepdim=True)
        _, vecs = torch.linalg.eigh(flat @ flat.T / flat.shape[1])
        basis = vecs[:, -3:].flip(1)
        if self.basis is not None:  # keep colours steady from step to step
            signs = torch.sign((basis * self.basis).sum(dim=0))
            basis = basis * torch.where(signs == 0, torch.ones_like(signs), signs)
        self.basis = basis
        rgb = (basis.T @ flat).reshape(3, h, w)
        lo = torch.quantile(rgb.reshape(3, -1), 0.02, dim=1)[:, None, None]
        hi = torch.quantile(rgb.reshape(3, -1), 0.98, dim=1)[:, None, None]
        return ((rgb - lo) / (hi - lo + 1e-6)).clamp(0, 1)

    def frame(self, final=False):
        """A JPEG of the current guess, or None if nothing new is ready."""
        if self.x0 is None:
            return None
        now = time.time()
        if not final and now - self.last < EVERY_S:
            return None
        self.last = now
        with torch.no_grad():
            x = self.x0[0].float()
            if x.dim() == 2:
                x = self._unpack(x)
                if x is None:
                    return None
            while x.dim() > 3:
                x = x[:, 0]
            factors = FACTORS.get(self.family)
            if factors and len(factors[0]) == x.shape[0]:
                m = torch.tensor(factors[0], device=x.device)
                b = torch.tensor(factors[1], device=x.device)[:, None, None]
                rgb = ((torch.einsum("chw,cr->rhw", x, m) + b + 1) / 2).clamp(0, 1)
            else:
                rgb = self._false_colour(x)
            pixels = (rgb.permute(1, 2, 0) * 255).round().byte().cpu().numpy()
        image = Image.fromarray(pixels)
        scale = SIZE / max(image.size)
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.BILINEAR)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=82)
        return out.getvalue()
