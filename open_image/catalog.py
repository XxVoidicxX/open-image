import os
import time
from dataclasses import dataclass

from .paths import MODELS

ANIME_NEGATIVE = "lowres, bad anatomy, bad hands, text, error, worst quality, low quality, jpeg artifacts, watermark, blurry"
MARKER = ".open-image-complete"
GIB = 2**30


def folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    family: str  # picks the loader in engines.py
    repo: str  # Hugging Face repo
    base: int  # side length of the square pixel budget
    steps: tuple  # fast, balanced, best
    guidance: float
    blurb: str
    tags: tuple
    symbol: str
    hue: int
    example: str
    size_gb: float  # approximate download size
    uncensored: bool = False
    negative: bool = True  # does the model take a negative prompt
    default_negative: str = ""
    prefix: str = ""  # tags the model was trained on, added when auto-tune is on
    need_ram: float = 5.0  # GiB of free RAM it is comfortable with
    overhead_s: int = 0  # paid on every image by models that reload their text encoder
    load_s: int = 30
    unit: float = 1.0  # seconds per megapixel per step on an RTX 3050 8 GB
    allow: tuple = ()
    ignore: tuple = ()

    @property
    def path(self):
        return MODELS / self.id

    def _has_weights(self):
        if self.family == "sdxl-single":
            return any(self.path.glob("*.safetensors"))
        return (self.path / "model_index.json").exists()

    def _clear_stale_partials(self):
        # interrupted downloads leave unfinished files behind even after a later run completed
        for f in (self.path / ".cache" / "huggingface" / "download").glob("**/*.incomplete"):
            if time.time() - f.stat().st_mtime > 3600:
                f.unlink(missing_ok=True)

    @property
    def installed(self):
        if not self.path.exists():
            return False
        if (self.path / MARKER).exists():
            return True
        # a model downloaded before the marker existed: accept it if it is about the full size
        if self._has_weights() and folder_size(self.path) >= 0.85 * self.size_gb * GIB:
            self._clear_stale_partials()
            (self.path / MARKER).touch()
            return True
        return False


_FP16_ONLY = ("model_index.json", "*/config.json", "tokenizer*/*", "scheduler/*", "*/*.fp16.safetensors")
_SDXL_FOLDERS = ("*.ckpt", "*.bin", "*/*.fp16.bin")

MODEL_LIST = [
    Model(
        id="qwen-image-21", name="Qwen Image 2.1", family="qwen", repo="Qwen/Qwen-Image-2.1", base=1024, steps=(20, 30, 40), guidance=1.0,
        blurb="Newest and sharpest. Best at readable text and realistic detail. The slowest, and it reloads for every image.",
        tags=("Best quality", "Great at text", "Slow"), symbol="Q", hue=265, negative=False, need_ram=6.0, overhead_s=55, load_s=0, unit=4.95,
        example="a neon shop sign that reads \"OPEN LATE\", rainy night, reflections on wet pavement",
        size_gb=31, ignore=("*.md",),
    ),
    Model(
        id="flux2-klein-4b", name="FLUX.2 Klein 4B", family="flux2", repo="black-forest-labs/FLUX.2-klein-4B", base=1024, steps=(2, 4, 6), guidance=1.0,
        blurb="Very fast and photorealistic. The best balance of speed and quality on a mid-range card.",
        tags=("Fast", "Photoreal"), symbol="F", hue=190, negative=False, need_ram=5.0, load_s=50, unit=3.7,
        example="candid photo of a street musician at golden hour, shallow depth of field",
        size_gb=15, ignore=("flux-2-klein-4b.safetensors",),
    ),
    Model(
        id="z-image-turbo", name="Z-Image Turbo", family="zimage", repo="Tongyi-MAI/Z-Image-Turbo", base=1024, steps=(6, 9, 12), guidance=0.0,
        blurb="Quick all-rounder with clean, crisp detail and good prompt following.",
        tags=("Fast", "All-round"), symbol="Z", hue=150, negative=False, need_ram=6.0, load_s=50, unit=4.9,
        example="a cozy cabin in a snowy pine forest at dusk, warm light in the windows",
        size_gb=33, ignore=("*.ckpt",),
    ),
    Model(
        id="chroma1-hd", name="Chroma HD", family="chroma", repo="lodestones/Chroma1-HD", base=896, steps=(16, 26, 36), guidance=3.0,
        blurb="A FLUX-style model with no built-in content filter and a wide creative range. Reloads its text encoder for every image.",
        tags=("Uncensored", "Creative"), symbol="C", hue=25, uncensored=True, need_ram=6.0, overhead_s=35, load_s=0, unit=10.8,
        default_negative="low quality, ugly, unfinished, out of focus, deformed, disfigured, blurry, watermark",
        example="surreal oil painting of a lighthouse growing out of a whale, stormy sea",
        size_gb=26, ignore=("Chroma1-HD.safetensors",),
    ),
    Model(
        id="pony-v6-xl", name="Pony Diffusion V6 XL", family="sdxl-single", repo="LyliaEngine/Pony_Diffusion_V6_XL", base=1024, steps=(18, 28, 40), guidance=7.0,
        blurb="Stylized and anime-leaning art that understands tag-style prompts. No built-in content filter.",
        tags=("Uncensored", "Anime / stylized"), symbol="P", hue=312, uncensored=True, need_ram=5.0, load_s=12, unit=1.15,
        prefix="score_9, score_8_up, score_7_up, ", default_negative="score_6, score_5, score_4, " + ANIME_NEGATIVE,
        example="1girl, mature female, silver hair, red eyes, black jacket, cyberpunk city at night, neon lights, rain",
        size_gb=7, allow=("ponyDiffusionV6XL_v6StartWithThisOne.safetensors",),
    ),
    Model(
        id="prefect-pony-xl", name="Prefect Pony XL v5", family="sdxl-anime", repo="John6666/prefect-pony-xl-v50-sdxl", base=1024, steps=(18, 28, 40), guidance=6.5,
        blurb="A refined Pony-based model with cleaner anatomy and lighting. Same score tags as Pony. No built-in content filter.",
        tags=("Uncensored", "Anime / stylized"), symbol="Pf", hue=290, uncensored=True, need_ram=5.0, load_s=12, unit=1.15,
        prefix="score_9, score_8_up, score_7_up, ", default_negative="score_6, score_5, score_4, " + ANIME_NEGATIVE,
        example="1girl, mature female, red hair, leather jacket, rooftop at sunset, city skyline, detailed background",
        size_gb=6.5, ignore=_SDXL_FOLDERS,
    ),
    Model(
        id="wai-illustrious", name="WAI Illustrious v14", family="sdxl-anime", repo="John6666/wai-nsfw-illustrious-sdxl-v140-sdxl", base=1024, steps=(18, 28, 40), guidance=6.0,
        blurb="Polished anime illustration with clean line work. Tag-style prompts work best. No built-in content filter.",
        tags=("Uncensored", "Anime"), symbol="W", hue=346, uncensored=True, need_ram=5.0, load_s=12, unit=1.15,
        prefix="masterpiece, best quality, ", default_negative=ANIME_NEGATIVE,
        example="1girl, mature female, long white hair, kimono, cherry blossoms, sunset, looking back",
        size_gb=6.7, ignore=("*.ckpt", "*.bin"),
    ),
    Model(
        id="noobai-xl", name="NoobAI XL 1.1", family="sdxl-anime", repo="Laxhar/noobai-XL-1.1", base=1024, steps=(18, 28, 40), guidance=5.5,
        blurb="Illustrious-based anime model trained on a huge tag set, great at characters and styles. No built-in content filter.",
        tags=("Uncensored", "Anime"), symbol="N", hue=170, uncensored=True, need_ram=5.0, load_s=12, unit=1.15,
        prefix="masterpiece, best quality, newest, absurdres, highres, ", default_negative=ANIME_NEGATIVE + ", old, early, bad quality",
        example="1girl, mature female, blue eyes, short black hair, hoodie, night street, neon signs, looking at viewer",
        size_gb=6.5, ignore=("NoobAI-XL-v1.1.safetensors", "*.ckpt", "*.bin"),
    ),
    Model(
        id="sdxl-base", name="Stable Diffusion XL 1.0", family="sdxl", repo="stabilityai/stable-diffusion-xl-base-1.0", base=1024, steps=(15, 25, 40), guidance=6.5,
        blurb="The dependable classic. Good general quality, but it needs more effort in the prompt than newer models.",
        tags=("All-round",), symbol="XL", hue=215, need_ram=5.0, load_s=12, unit=1.2,
        default_negative="blurry, low quality, watermark",
        example="a vast fantasy castle on a floating island above the clouds at sunset",
        size_gb=6.7, allow=_FP16_ONLY,
    ),
    Model(
        id="sd15", name="Stable Diffusion 1.5", family="sd15", repo="stable-diffusion-v1-5/stable-diffusion-v1-5", base=512, steps=(15, 28, 40), guidance=7.0,
        blurb="Tiny and very fast, but lower detail. Good for rough ideas and quick experiments.",
        tags=("Lightest", "Very fast"), symbol="1.5", hue=45, need_ram=3.0, load_s=16, unit=0.82,
        default_negative="blurry, low quality, watermark, deformed",
        example="a watercolor painting of a fox in an autumn forest",
        size_gb=2.6, allow=_FP16_ONLY,
    ),
]
BY_ID = {m.id: m for m in MODEL_LIST}

ASPECTS = ("square", "wide", "tall")
QUALITIES = ("fast", "balanced", "best")


def wide_size(side):
    area = side * side
    h = round((area / (16 / 9)) ** 0.5 / 16) * 16
    w = round(h * 16 / 9 / 16) * 16
    return w, h


def size_for(model, aspect):
    if aspect == "wide":
        return wide_size(model.base)
    if aspect == "tall":
        w, h = wide_size(model.base)
        return h, w
    return model.base, model.base
