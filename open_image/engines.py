"""Model loaders. Each engine owns one model and turns a request dict into a PIL image."""
import gc

import torch

from .preview import Preview


def free_memory():
    gc.collect()
    torch.cuda.empty_cache()


def skip_allocator_warmup():
    """Both diffusers and transformers pre-allocate a block the size of the largest weight before loading.
    Under the VRAM cap that allocation alone can fail, so turn it off."""
    try:
        import diffusers.models.model_loading_utils as dl
        dl._caching_allocator_warmup = lambda *a, **k: None
    except Exception:
        pass
    try:
        import transformers.modeling_utils as tl
        tl.caching_allocator_warmup = lambda *a, **k: None
    except Exception:
        pass


def _nf4_pipeline_config():
    from diffusers import PipelineQuantizationConfig
    return PipelineQuantizationConfig(
        quant_backend="bitsandbytes_4bit",
        quant_kwargs={"load_in_4bit": True, "bnb_4bit_quant_type": "nf4", "bnb_4bit_compute_dtype": torch.bfloat16},
        components_to_quantize=["transformer", "text_encoder"],
    )


def _nf4_transformers():
    from transformers import BitsAndBytesConfig
    return BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)


def _nf4_diffusers():
    from diffusers import BitsAndBytesConfig
    return BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)


class Engine:
    def __init__(self, model, emit):
        self.model = model
        self.emit = emit
        self.preview = None

    def watch(self, pipe, req):
        self.preview = Preview(self.model.family, req["width"], req["height"])
        self.preview.attach(pipe)

    def release(self):
        pass


class Standard(Engine):
    """Models that fit once quantized or offloaded. Loaded once, reused for every request."""

    def __init__(self, model, emit):
        super().__init__(model, emit)
        self.pipe = self._load()

    def _load(self):
        from diffusers import (DiffusionPipeline, EulerAncestralDiscreteScheduler, StableDiffusionPipeline,
                               StableDiffusionXLPipeline)
        m, fam, path = self.model, self.model.family, str(self.model.path)
        resident = False
        if fam == "sd15":
            pipe = StableDiffusionPipeline.from_pretrained(path, torch_dtype=torch.float16, variant="fp16", safety_checker=None,
                                                           feature_extractor=None, requires_safety_checker=False)
            resident = True
        elif fam == "sdxl":
            pipe = StableDiffusionXLPipeline.from_pretrained(path, torch_dtype=torch.float16, variant="fp16")
        elif fam == "sdxl-anime":
            pipe = StableDiffusionXLPipeline.from_pretrained(path, torch_dtype=torch.float16)
            pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
        elif fam == "sdxl-single":
            ckpt = next(m.path.glob("*.safetensors"))
            pipe = StableDiffusionXLPipeline.from_single_file(str(ckpt), torch_dtype=torch.float16)
            pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
        elif fam == "zimage":
            from diffusers import ZImagePipeline
            pipe = ZImagePipeline.from_pretrained(path, torch_dtype=torch.bfloat16, quantization_config=_nf4_pipeline_config())
        elif fam == "flux2":
            pipe = DiffusionPipeline.from_pretrained(path, torch_dtype=torch.bfloat16, quantization_config=_nf4_pipeline_config())
        else:
            raise ValueError(f"unknown family {fam}")
        if resident:
            pipe.to("cuda")
        else:
            pipe.enable_model_cpu_offload()
            pipe.vae.enable_tiling()
        return pipe

    def run(self, req, callback):
        m = self.model
        kwargs = dict(prompt=req["prompt"], width=req["width"], height=req["height"], num_inference_steps=req["steps"],
                      guidance_scale=m.guidance, generator=torch.Generator("cpu").manual_seed(req["seed"]))
        if m.negative and req.get("negative"):
            kwargs["negative_prompt"] = req["negative"]
        self.emit(type="phase", id=req["id"], phase="generating")
        self.watch(self.pipe, req)
        try:
            return self.pipe(callback_on_step_end=callback, **kwargs).images[0]
        except TypeError:
            return self.pipe(**kwargs).images[0]


class Chroma(Engine):
    """The text encoder and transformer don't fit on the GPU together, so encode first and free it."""

    def __init__(self, model, emit):
        super().__init__(model, emit)
        self.pipe = None

    def _encode(self, prompt, negative):
        from diffusers import ChromaPipeline
        from transformers import T5EncoderModel
        path = str(self.model.path)
        te = T5EncoderModel.from_pretrained(path + "/text_encoder", torch_dtype=torch.bfloat16, low_cpu_mem_usage=True,
                                            quantization_config=_nf4_transformers())
        pipe = ChromaPipeline.from_pretrained(path, transformer=None, vae=None, text_encoder=te, torch_dtype=torch.bfloat16)
        with torch.no_grad():
            pe, _, pm, ne, _, nm = pipe.encode_prompt(prompt=prompt, negative_prompt=negative or self.model.default_negative,
                                                      device="cuda", do_classifier_free_guidance=True)
        out = dict(prompt_embeds=pe.cpu(), prompt_attention_mask=pm.cpu(),
                   negative_prompt_embeds=ne.cpu(), negative_prompt_attention_mask=nm.cpu())
        del pipe, te
        free_memory()
        return out

    def _load_transformer(self):
        from diffusers import ChromaPipeline, ChromaTransformer2DModel
        path = str(self.model.path)
        tr = ChromaTransformer2DModel.from_pretrained(path, subfolder="transformer", torch_dtype=torch.bfloat16,
                                                      quantization_config=_nf4_diffusers(), device_map="cuda")
        pipe = ChromaPipeline.from_pretrained(path, transformer=tr, text_encoder=None, tokenizer=None, torch_dtype=torch.bfloat16)
        pipe.enable_model_cpu_offload()
        pipe.vae.enable_tiling()
        return pipe

    def run(self, req, callback):
        self.emit(type="phase", id=req["id"], phase="encoding")
        emb = self._encode(req["prompt"], req.get("negative"))
        if self.pipe is None:
            self.emit(type="phase", id=req["id"], phase="loading")
            self.pipe = self._load_transformer()
        self.emit(type="phase", id=req["id"], phase="generating")
        emb = {k: v.to("cuda") for k, v in emb.items()}
        self.watch(self.pipe, req)
        return self.pipe(width=req["width"], height=req["height"], num_inference_steps=req["steps"], guidance_scale=self.model.guidance,
                         generator=torch.Generator("cpu").manual_seed(req["seed"]), callback_on_step_end=callback, **emb).images[0]

    def release(self):
        # the next prompt needs the room back for the text encoder
        self.pipe = None
        free_memory()


class Qwen(Engine):
    """Same two-step idea as Chroma. The transformer is loaded fresh for each image."""

    def __init__(self, model, emit):
        super().__init__(model, emit)
        self.pipe = None

    def _encode(self, prompt):
        from diffusers import QwenImage21Pipeline
        from transformers import Qwen3VLForConditionalGeneration
        path = str(self.model.path)
        te = Qwen3VLForConditionalGeneration.from_pretrained(path + "/text_encoder", torch_dtype=torch.bfloat16, low_cpu_mem_usage=True,
                                                             quantization_config=_nf4_transformers())
        pipe = QwenImage21Pipeline.from_pretrained(path, transformer=None, vae=None, text_encoder=te, torch_dtype=torch.bfloat16)
        with torch.no_grad():
            embeds, mask, _ = pipe.encode_prompt(prompt=prompt, device="cuda")
        out = (embeds.cpu(), None if mask is None else mask.cpu())
        del pipe, te
        free_memory()
        return out

    def run(self, req, callback):
        from diffusers import QwenImage21Pipeline, QwenImage21Transformer2DModel
        path = str(self.model.path)
        self.emit(type="phase", id=req["id"], phase="encoding")
        embeds, mask = self._encode(req["prompt"])
        self.emit(type="phase", id=req["id"], phase="loading")
        tr = QwenImage21Transformer2DModel.from_pretrained(path, subfolder="transformer", torch_dtype=torch.bfloat16,
                                                           quantization_config=_nf4_diffusers(), device_map="cuda")
        self.pipe = QwenImage21Pipeline.from_pretrained(path, transformer=tr, text_encoder=None, torch_dtype=torch.bfloat16)
        self.pipe.enable_model_cpu_offload()
        self.pipe.vae.enable_tiling()
        self.emit(type="phase", id=req["id"], phase="generating")
        self.watch(self.pipe, req)
        return self.pipe(prompt_embeds=embeds.to("cuda"), prompt_embeds_mask=None if mask is None else mask.to("cuda"),
                         width=req["width"], height=req["height"], num_inference_steps=req["steps"],
                         generator=torch.Generator("cpu").manual_seed(req["seed"]), callback_on_step_end=callback).images[0]

    def release(self):
        self.pipe = None
        free_memory()


def create(model, emit):
    if model.family == "chroma":
        return Chroma(model, emit)
    if model.family == "qwen":
        return Qwen(model, emit)
    return Standard(model, emit)
