# Open Image

A local image generator with a chat interface. Pick a model, type a prompt, wait for the picture. It runs on your own GPU, keeps your chats on your own disk, never writes your pictures to disk, and works offline once the models are downloaded.

It is built for mid-range cards. Everything here was developed and tested on an RTX 3050 with 8 GB of VRAM and 16 GB of RAM, so the larger models are loaded in 4-bit and swapped between GPU and system memory as needed. They are slower than on a big card, but they run.

![Open Image](docs/screenshot.png)

## Features

- Pictures are never written to disk. Each one is encrypted with AES-256-GCM under a random key made when the app starts, and kept in memory only. See [Privacy](#privacy).
- Chat-style interface. Chats are saved on your machine, can be renamed, and remember the model last used in each.
- An Images page with everything you have made since the app started, from every chat. Filter by model, open any image for its prompt and settings, reuse them, or delete it.
- Ten models, installed and removed from inside the app. A filter shows the uncensored ones, including the ones you haven't downloaded yet.
- A wait estimate for every model before you send. It learns from your own machine: each finished image is recorded together with how busy the GPU was with other programs, free RAM and GPU temperature, and later estimates lean on the runs that happened under similar conditions. It starts from defaults measured on an RTX 3050 and gets more accurate the more you use it.
- Square, 16:9 wallpaper, and 9:16 phone shapes. Fast, balanced, and best quality presets.
- Guard rails so a heavy model can't freeze the PC: GPU memory is capped, a watchdog stops the run if the system starts paging heavily, and a hot GPU gets paused between steps.
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

Start the app:

```
python -m open_image
```

On Windows you can also double-click `run.bat`. The app opens in its own window; if Edge isn't installed it opens in your default browser at http://127.0.0.1:7860.

Models are installed from the app: open the model picker at the bottom right of the chat box and choose Manage models. You can also use the command line:

```
python -m open_image models
python -m open_image download flux2-klein-4b z-image-turbo
```

## Models

| Model | Id | Download | Notes |
|---|---|---|---|
| Qwen Image 2.1 | `qwen-image-21` | ~31 GB | Best quality and text. Slowest, reloads for every image |
| FLUX.2 Klein 4B | `flux2-klein-4b` | ~15 GB | Fast, photorealistic |
| Z-Image Turbo | `z-image-turbo` | ~33 GB | Fast all-rounder |
| Chroma HD | `chroma1-hd` | ~26 GB | FLUX-style, uncensored |
| Pony Diffusion V6 XL | `pony-v6-xl` | ~7 GB | Stylized and anime, tag prompts, uncensored |
| Prefect Pony XL v5 | `prefect-pony-xl` | ~6.5 GB | Pony-based, cleaner anatomy, uncensored |
| WAI Illustrious v14 | `wai-illustrious` | ~7 GB | Anime illustration, tag prompts, uncensored |
| NoobAI XL 1.1 | `noobai-xl` | ~6.5 GB | Illustrious-based anime, huge tag set, uncensored |
| Stable Diffusion XL 1.0 | `sdxl-base` | ~7 GB | The classic |
| Stable Diffusion 1.5 | `sd15` | ~3 GB | Tiny and very fast |

Models come from Hugging Face and each keeps its own license. Check the model page before using output commercially.

"Uncensored" here means the model has no built-in content filter and was trained on a broad range of material. Open Image doesn't add a filter of its own, so what you make with them is up to you.

Typical times on an RTX 3050 8 GB at the default size, balanced quality: Stable Diffusion 1.5 about 20 s, SDXL and the anime models about 35 s, FLUX.2 Klein about 15 s once loaded, Z-Image Turbo about 45 s, Chroma HD about 4 min, Qwen Image 2.1 about 3 min plus about a minute of loading.

## Privacy

Generated pictures exist in three places only: the memory of the model process while it draws, the encrypted store in the app's memory, and the browser window while you look at them. They are never written to disk by the app.

- A random 256-bit key is created each time the app starts and is held only in memory. Every picture and preview is sealed with AES-256-GCM, with a fresh random nonce, and decrypted only when the window asks for it.
- Pictures are sent to the window with `Cache-Control: no-store`, and the app window runs with its own browser profile and the disk cache turned off.
- The server only answers requests addressed to `127.0.0.1` or `localhost`.
- When the app closes the key is gone, and so are the pictures. Use Save on an image to put a copy in your downloads folder; that is the only way a picture ever becomes a file.
- Chats keep their prompts and settings so you can reuse them. After a restart the picture itself shows as cleared.

What this does not cover: the operating system can move any program's memory into its page file or hibernation file, which the app cannot prevent. Turn on full-disk encryption (BitLocker) if that matters to you. Screenshots and files you save yourself are outside the app's control.

Older versions saved pictures in an `images` folder. If the app finds files there it offers to delete them.

## Where things are stored

Everything lives under one folder, `~/OpenImage` by default:

```
models/   downloaded models
data/     chats, prompt history, timing data, logs
```

Set `OPEN_IMAGE_HOME` to use a different folder, for example a bigger drive. `OPEN_IMAGE_PORT` changes the port.

## How it works

The web server (FastAPI) never loads a model itself. Each model runs in a separate worker process that talks to the server over a pipe. If a model runs out of memory or the guard stops it, only that worker dies, and the app reports what happened. Model downloads also run as their own process, so they can be cancelled and resumed.

- `open_image/catalog.py` is the model list: names, settings, download rules.
- `open_image/engines.py` has the loaders for each model family.
- `open_image/guard.py` holds the memory and temperature limits.
- `open_image/timing.py` and `resources.py` produce the wait estimates.
- `open_image/installs.py` installs and removes models.
- `open_image/jobs.py` is the queue, chat storage, and worker management.

To add a model, add an entry to `catalog.py` and, if it is a new architecture, a loader in `engines.py`.

## Status

Version 0.3. Not done yet: image-to-image and editing, and testing on Linux.

## License

MIT. See `LICENSE`.
