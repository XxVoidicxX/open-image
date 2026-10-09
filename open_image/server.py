import hmac
import json
import os
import random
import secrets
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, resources, timing
from .catalog import ASPECTS, BY_ID, MODEL_LIST, QUALITIES, folder_size, size_for
from .installs import Installer
from .jobs import Manager
from .locks import BadPin, Locked, TooFast
from .paths import DATA, LEGACY_IMAGES, LEGACY_THUMBS
from .vault import vault

STATIC = Path(__file__).parent / "static"
PORT = int(os.environ.get("OPEN_IMAGE_PORT", "7860"))
PREFS_FILE = DATA / "prefs.json"
COOKIE = "oi_session"
# the window gets this secret on launch; anything else on the machine has to go without
TOKEN = os.environ.get("OPEN_IMAGE_TOKEN") or secrets.token_urlsafe(32)
CSP = ("default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'")

manager = Manager()
try:
    vault.set_keep(json.loads(PREFS_FILE.read_text(encoding="utf-8")).get("keep_pictures", "1") != "0")
except (OSError, ValueError):
    pass
installer = Installer()
resources.monitor.busy = lambda: manager.busy
app = FastAPI(title="Open Image", version=__version__, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def gate(request, call_next):
    if hmac.compare_digest(request.query_params.get("t", ""), TOKEN):
        reply = RedirectResponse(request.url.path or "/")
        reply.set_cookie(COOKIE, TOKEN, httponly=True, samesite="strict")
        return reply
    if not hmac.compare_digest(request.cookies.get(COOKIE, ""), TOKEN):
        return JSONResponse({"detail": "This window is not connected to Open Image."}, status_code=401)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(Locked)
async def on_locked(request, exc):
    return JSONResponse({"detail": "This is locked. Enter its PIN first.", "locked": True}, status_code=423)


@app.exception_handler(BadPin)
async def on_bad_pin(request, exc):
    return JSONResponse({"detail": "That PIN is not right."}, status_code=403)


@app.exception_handler(TooFast)
async def on_too_fast(request, exc):
    seconds = int(exc.seconds) + 1
    return JSONResponse({"detail": f"Too many wrong tries. Wait {seconds} seconds and try again.", "wait": seconds}, status_code=429)


@app.exception_handler(ValueError)
async def on_value_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(KeyError)
async def on_missing(request, exc):
    return JSONResponse({"detail": "Not found."}, status_code=404)


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


class PinBody(BaseModel):
    pin: str = ""
    new_pin: str = ""
    label: str = ""


class Item(BaseModel):
    job_id: str


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
def images(model: str | None = None, folder: str | None = None):
    return manager.images(model, folder)


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
    data = vault.get(job_id, "full" if ext == "png" else "thumb", manager.keys)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/png" if ext == "png" else "image/jpeg")


@app.get("/api/images/{job_id}/download")
def download_image(job_id: str):
    data = vault.get(job_id, "full", manager.keys)
    job = manager.find_job(job_id)
    if data is None or job is None:
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
    if chat:
        manager.require_open_chat(chat["id"])
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


@app.get("/api/jobs/{job_id}/preview")
def job_preview(job_id: str):
    job = manager.jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    manager.require_open_chat(job["chat_id"])
    data = manager.previews.get(job_id)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg")


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


@app.post("/api/lock/{kind}/{item_id}/{action}")
def lock_action(kind: str, item_id: str, action: str, body: PinBody):
    if kind not in ("chat", "folder") or action not in ("protect", "unlock", "relock", "unprotect", "repin"):
        raise HTTPException(404)
    if action == "protect":
        manager.protect(kind, item_id, body.pin, body.label)
    elif action == "unlock":
        manager.unlock(kind, item_id, body.pin)
    elif action == "relock":
        manager.relock(kind, item_id)
    elif action == "unprotect":
        manager.unprotect(kind, item_id, body.pin)
    else:
        manager.repin(kind, item_id, body.pin, body.new_pin)
    return {"ok": True}


@app.get("/api/master")
def master_info():
    return manager.master_state()


@app.post("/api/master/{action}")
def master_action(action: str, body: PinBody):
    if action == "set":
        manager.master_set(body.new_pin or body.pin, body.pin if manager.master.exists else "")
    elif action == "unlock":
        manager.master_unlock(body.pin)
    elif action == "remove":
        manager.master_remove(body.pin)
    elif action == "lock":
        return {"ok": True, "busy": manager.lock_all()}
    else:
        raise HTTPException(404)
    return manager.master_state()


@app.get("/api/folders")
def folders():
    return manager.folder_list()


@app.post("/api/folders")
def new_folder(body: Rename):
    return {"id": manager.new_folder(body.title)["id"]}


@app.post("/api/folders/{folder_id}/rename")
def rename_folder(folder_id: str, body: Rename):
    manager.rename_folder(folder_id, body.title)
    return {"ok": True}


@app.post("/api/folders/{folder_id}/delete")
def delete_folder(folder_id: str):
    manager.delete_folder(folder_id)
    return {"ok": True}


@app.post("/api/folders/{folder_id}/add")
def folder_add(folder_id: str, body: Item):
    manager.add_to_folder(folder_id, body.job_id)
    return {"ok": True}


@app.post("/api/folders/{folder_id}/remove")
def folder_remove(folder_id: str, body: Item):
    manager.remove_from_folder(folder_id, body.job_id)
    return {"ok": True}


@app.post("/api/keep")
def keep_pictures(body: dict):
    on = bool(body.get("on"))
    vault.set_keep(on)
    set_prefs({"keep_pictures": "1" if on else "0"})
    return {"keep": vault.keep}


@app.get("/api/prefs")
def get_prefs():
    try:
        return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@app.post("/api/prefs")
def set_prefs(body: dict):
    prefs = get_prefs()
    prefs.update({str(k)[:40]: str(v)[:80] for k, v in body.items() if v is not None})
    PREFS_FILE.write_text(json.dumps(prefs), encoding="utf-8")
    return prefs


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


def bring_to_front():
    """If the app is already running, raise its window instead of starting a second copy."""
    if os.name != "nt":
        return
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, "Open Image")
    if hwnd:
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)


def set_icon():
    """Give the window and its taskbar button the app's own icon instead of Python's."""
    if os.name != "nt":
        return
    import ctypes
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
    hwnd = user32.FindWindowW(None, "Open Image")
    path = str(STATIC / "icon.ico")
    for which, size in ((1, 256), (0, 32)):  # ICON_BIG, ICON_SMALL
        icon = user32.LoadImageW(None, path, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        if hwnd and icon:
            user32.SendMessageW(hwnd, 0x80, which, icon)  # WM_SETICON


def run_window(url):
    import webview
    if os.name == "nt":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OpenImage.App")  # own taskbar group and icon
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    webview.settings["ALLOW_FILE_URLS"] = False
    window = webview.create_window("Open Image", f"{url}/?t={TOKEN}", width=1320, height=860, min_size=(900, 620), background_color="#06070b")
    window.events.shown += set_icon
    webview.start(gui="edgechromium", private_mode=True, debug=False)  # private mode: no profile, cache or cookies are kept


def serve(window=True):
    import uvicorn
    url = f"http://127.0.0.1:{PORT}"
    if sys.stdout is None or sys.stderr is None:  # pythonw has no console
        sys.stdout = sys.stderr = open(DATA / "server.log", "a", buffering=1, encoding="utf-8")
    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            bring_to_front()
            return
    resources.monitor.start()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started and thread.is_alive():
        time.sleep(0.05)
    try:
        if window:
            run_window(url)
        else:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        manager.lock_all()
        manager.stop_worker()
