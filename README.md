# Open Image

A local image generator with a chat interface. Pick a model, type a prompt, wait for the picture. It runs on your own GPU, keeps your chats on your own disk, and works offline once the models are downloaded.

It is built for mid-range cards. Everything here was developed and tested on an RTX 3050 with 8 GB of VRAM and 16 GB of RAM, so the larger models are loaded in 4-bit and swapped between GPU and system memory as needed. They are slower than on a big card, but they run.

![Open Image](docs/screenshot.png)

## Features

- Chat-style interface. Chats are saved on your machine, can be renamed, and remember the model last used in each.
- Eight models to choose from, from a 4 second Stable Diffusion 1.5 up to Qwen Image 2.1. See the table below.
- A wait estimate for every model before you send. It starts from measured speeds and then follows what your own machine actually does, adjusted for other programs using the GPU, GPU temperature, free RAM, and jobs already queued.
- Square, 16:9 wallpaper, and 9:16 phone shapes. Fast, balanced, and best quality presets.
- Guard rails so a heavy model can't freeze the PC: GPU memory is capped, a watchdog stops the run if RAM is about to run out, and a hot GPU gets paused between steps.
- Jobs are queued and run one at a time. One model stays in memory and is released after 15 minutes idle.

## Requirements

- Windows 10/11 (Linux should work but is untested)
- An NVIDIA GPU with 8 GB or more of VRAM
- 16 GB of RAM recommended
- Python 3.10 or newer
- Disk space for the models you pick (see the table)

## Install

```
git clone https://github.com/XxVoidicxX/open-image
cd open-image
python -m venv .venv
.venv\Scripts\activate
pip install torch --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt
```

Download the models you want. `python -m open_image models` lists them, and `all` fetches everything.

```
python -m open_image download flux2-klein-4b z-image-turbo
```

Start the app:

```
python -m open_image
```

On Windows you can also double-click `run.bat`. The app opens in its own window; if Edge isn't installed it opens in your default browser at http://127.0.0.1:7860.

## Models

| Model | Id | Download | Notes |
|---|---|---|---|
| Qwen Image 2.1 | `qwen-image-21` | ~31 GB | Best quality and text. Slowest, reloads for every image |
| FLUX.2 Klein 4B | `flux2-klein-4b` | ~15 GB | Fast, photorealistic |
| Z-Image Turbo | `z-image-turbo` | ~33 GB | Fast all-rounder |
| Chroma HD | `chroma1-hd` | ~26 GB | FLUX-style, no built-in content filter |
| Pony Diffusion V6 XL | `pony-v6-xl` | ~7 GB | Stylized and anime, tag prompts, no built-in content filter |
| WAI Illustrious v14 | `wai-illustrious` | ~7 GB | Anime illustration, tag prompts, no built-in content filter |
| Stable Diffusion XL 1.0 | `sdxl-base` | ~7 GB | The classic |
| Stable Diffusion 1.5 | `sd15` | ~3 GB | Tiny and very fast |

Models come from Hugging Face and each keeps its own license. Check the model page before using output commercially. Several of these models have no content filter of their own, and Open Image doesn't add one, so what you make with them is up to you.

Typical times on an RTX 3050 8 GB at the default size, balanced quality: Stable Diffusion 1.5 about 20 s, SDXL and the anime models about 35 s, FLUX.2 Klein about 15 s once loaded, Z-Image Turbo about 45 s, Chroma HD about 4 min, Qwen Image 2.1 about 3 min plus about a minute of loading.

## Where things are stored

Everything lives under one folder, `~/OpenImage` by default:

```
models/   downloaded models
images/   generated pictures
data/     chats, history, timing data, logs
```

Set `OPEN_IMAGE_HOME` to use a different folder, for example a bigger drive. `OPEN_IMAGE_PORT` changes the port.

## How it works

The web server (FastAPI) never loads a model itself. Each model runs in a separate worker process that talks to the server over a pipe. If a model runs out of memory or the guard stops it, only that worker dies, and the app reports what happened.

- `open_image/catalog.py` is the model list: names, settings, download rules.
- `open_image/engines.py` has the loaders for each model family.
- `open_image/guard.py` holds the memory and temperature limits.
- `open_image/timing.py` and `resources.py` produce the wait estimates.
- `open_image/jobs.py` is the queue, chat storage, and worker management.

To add a model, add an entry to `catalog.py` and, if it is a new architecture, a loader in `engines.py`.

## Status

Version 0.1. Things that are known to be missing: a model download button in the app (use the command line for now), image-to-image and editing, and Linux testing.

## License

MIT. See `LICENSE`.
