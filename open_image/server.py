import os
import random
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, resources, timing
from .catalog import ASPECTS, BY_ID, MODEL_LIST, QUALITIES, folder_size, size_for
from .installs import Installer
from .jobs import Manager
from .paths import DATA, LEGACY_IMAGES, LEGACY_THUMBS, WINDOW
from .vault import vault

STATIC = Path(__file__).parent / "static"
PORT = int(os.environ.get("OPEN_IMAGE_PORT", "7860"))

manager = Manager()
installer = Installer()
resources.monitor.busy = lambda: manager.busy
app = FastAPI(title="Open Image", version=__version__, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def never_cache(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


class Generate(BaseModel):
    model: str
    prompt: str
    negative: str = ""
    aspect: str = "square"
    quality: str = "balanced"
    seed: int | None = None
    enhance: bool = True
    chat_id: str | None = None


class Rename(BaseModel):
    title: str


def model_info(m):
    downloading = installer.downloading(m.id)
    installed = not downloading and m.installed
    return {"id": m.id, "name": m.name, "symbol": m.symbol, "hue": m.hue, "blurb": m.blurb, "tags": m.tags, "negative": m.negative,
            "example": m.example, "available": installed, "uncensored": m.uncensored, "size_gb": m.size_gb,
            "disk_gb": round(folder_size(m.path) / 2**30, 1) if m.path.exists() else 0, "default_negative": m.default_negative,
            "install": f"python -m open_image download {m.id}", "download": installer.status(m)}


@app.get("/api/models")
def models():
    return [model_info(m) for m in MODEL_LIST]


@app.post("/api/models/{model_id}/install")
def install_model(model_id: str):
    model = BY_ID.get(model_id)
    if not model:
        raise HTTPException(404, "No such model.")
    if not installer.downloading(model_id) and model.installed:
        raise HTTPException(409, "Already installed.")
    installer.start(model)
    return {"ok": True}


@app.post("/api/models/{model_id}/cancel")
def cancel_install(model_id: str):
    installer.cancel(model_id)
    return {"ok": True}


@app.post("/api/models/{model_id}/uninstall")
def uninstall_model(model_id: str):
    model = BY_ID.get(model_id)
    if not model:
        raise HTTPException(404, "No such model.")
    if installer.downloading(model_id):
        raise HTTPException(409, "It is still downloading. Cancel the download first.")
    if manager.has_live_jobs(model_id):
        raise HTTPException(409, "A queued or running image is using this model.")
    if manager.model_id == model_id:
        manager.stop_worker()
    try:
        installer.uninstall(model)
    except OSError:
        raise HTTPException(500, "Some files are in use. Close anything using the model and try again.")
    return {"ok": True}


@app.get("/api/images")
def images(model: str | None = None):
    return manager.images(model)


@app.post("/api/images/{job_id}/delete")
def delete_image(job_id: str):
    if not manager.delete_image(job_id):
        raise HTTPException(404, "No such image.")
    return {"ok": True}


@app.get("/vault/{name}")
def vault_file(name: str):
    job_id, _, ext = name.partition(".")
    if not job_id.isalnum() or ext not in ("png", "jpg"):
        raise HTTPException(404)
    data = vault.get(job_id, "full" if ext == "png" else "thumb")
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/png" if ext == "png" else "image/jpeg")


@app.get("/api/images/{job_id}/download")
def download_image(job_id: str):
    job = manager.jobs.get(job_id)
    data = vault.get(job_id, "full") if job else None
    if data is None:
        raise HTTPException(404, "That image is no longer in memory.")
    return Response(data, media_type="image/png", headers={"Content-Disposition": f'attachment; filename="{job["model"]}-{job["seed"]}.png"'})


def leftovers():
    found = []
    for folder in (LEGACY_IMAGES, LEGACY_THUMBS):
        if folder.is_dir():
            found += [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    return found


@app.get("/api/leftovers")
def leftover_info():
    files = leftovers()
    return {"count": len(files), "folder": str(LEGACY_IMAGES)}


@app.post("/api/leftovers/delete")
def leftover_delete():
    for f in leftovers():
        f.unlink(missing_ok=True)
    for folder in (LEGACY_IMAGES, LEGACY_THUMBS):
        try:
            folder.rmdir()
        except OSError:
            pass
    return {"ok": True}


@app.get("/api/state")
def state(chat: str | None = None):
    snap = manager.snapshot(chat)
    snap["version"] = __version__
    snap["downloads"] = {m.id: st for m in MODEL_LIST if (st := installer.status(m))["state"] != "idle"}
    return snap


@app.post("/api/estimates")
def estimates(body: dict):
    aspect, quality = body.get("aspect", "square"), body.get("quality", "balanced")
    res = resources.read()
    return {m.id: timing.estimate(m, aspect, quality, manager.model_id, res) for m in MODEL_LIST}


@app.post("/api/generate")
def generate(req: Generate):
    model = BY_ID.get(req.model)
    if not model or installer.downloading(model.id) or not model.installed:
        raise HTTPException(400, "That model is not installed.")
    if vault.full:
        raise HTTPException(409, "The in-memory picture store is full. Delete a few images to make room.")
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(400, "Write a prompt first.")
    if req.aspect not in ASPECTS or req.quality not in QUALITIES:
        raise HTTPException(400, "Bad settings.")

    chat = manager.chats.get(req.chat_id) if req.chat_id else None
    if not chat:
        chat = manager.new_chat(model.id)
    manager.touch_chat(chat["id"], model.id, prompt)

    width, height = size_for(model, req.aspect)
    negative = req.negative.strip() or (model.default_negative if req.enhance else "")
    est = timing.estimate(model, req.aspect, req.quality, manager.model_id)
    job = {
        "id": uuid.uuid4().hex[:12], "chat_id": chat["id"], "created": time.time(), "model": model.id,
        "prompt": prompt, "negative": req.negative.strip(),
        "final_prompt": model.prefix + prompt if req.enhance else prompt, "final_negative": negative,
        "aspect": req.aspect, "quality": req.quality, "width": width, "height": height,
        "steps": model.steps[QUALITIES.index(req.quality)], "seed": req.seed if req.seed is not None else random.randint(0, 2**31 - 1),
        "status": "queued", "estimated_s": round(est["total_s"], 1), "step": 0,
    }
    manager.submit(job)
    return {"id": job["id"], "chat_id": chat["id"]}


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    return {"ok": manager.cancel(job_id)}


@app.post("/api/chats")
def new_chat(body: dict | None = None):
    return manager.new_chat((body or {}).get("model") or MODEL_LIST[0].id)


@app.post("/api/chats/{chat_id}/rename")
def rename_chat(chat_id: str, req: Rename):
    if not manager.rename_chat(chat_id, req.title):
        raise HTTPException(404, "No such chat.")
    return {"ok": True}


@app.post("/api/chats/{chat_id}/delete")
def delete_chat(chat_id: str):
    if not manager.delete_chat(chat_id):
        raise HTTPException(404, "No such chat.")
    return {"ok": True}


@app.post("/api/unload")
def unload():
    if manager.busy:
        raise HTTPException(409, "A job is running.")
    manager.stop_worker()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


def open_window(url):
    edge = [os.path.join(os.environ.get(var, ""), r"Microsoft\Edge\Application\msedge.exe")
            for var in ("ProgramFiles(x86)", "ProgramFiles")]
    for exe in edge:
        if os.path.exists(exe):  # app mode gives a window without browser chrome
            WINDOW.mkdir(parents=True, exist_ok=True)  # own profile with the disk cache switched off
            subprocess.Popen([exe, f"--app={url}", "--window-size=1320,860", f"--user-data-dir={WINDOW}",
                              "--disk-cache-size=1", "--media-cache-size=1", "--no-first-run", "--no-default-browser-check"])
            return
    webbrowser.open(url)


def serve(open_browser=True):
    url = f"http://127.0.0.1:{PORT}"
    if sys.stdout is None or sys.stderr is None:  # pythonw has no console
        sys.stdout = sys.stderr = open(DATA / "server.log", "a", buffering=1, encoding="utf-8")
    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:  # already running, just show it
            if open_browser:
                open_window(url)
            return
    resources.monitor.start()
    if open_browser:
        threading.Timer(1.2, open_window, args=(url,)).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    finally:
        manager.stop_worker()
