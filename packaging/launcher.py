"""OpenImage.exe: sets up the app on first run, then starts it.

The app needs PyTorch with CUDA, which is several gigabytes, so it can't ship inside one file. Instead this
small program installs a private Python and the libraries into %LOCALAPPDATA%\\OpenImage the first time it
runs (or after an update), copies the app code next to them, adds Start menu and desktop shortcuts, and then
launches the app. Later runs go straight to the app.
"""
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path

VERSION = "0.0.0"  # replaced at build time
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
ROOT = Path(os.environ.get("OPEN_IMAGE_RUNTIME") or Path(os.environ["LOCALAPPDATA"]) / "OpenImage")
VENV = ROOT / "runtime"
PYTHONW = VENV / "Scripts" / "pythonw.exe"
PYTHON = VENV / "Scripts" / "python.exe"
APP = ROOT / "app"
MARKER = ROOT / "installed.json"
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
NO_WINDOW = 0x08000000


def requirements():
    return (BUNDLE / "requirements.txt").read_text(encoding="utf-8")


def wanted():
    return {"version": VERSION, "requirements": hashlib.sha256(requirements().encode()).hexdigest()}


def ready():
    try:
        return PYTHONW.exists() and json.loads(MARKER.read_text(encoding="utf-8")) == wanted()
    except (OSError, ValueError):
        return False


def launch():
    env = dict(os.environ, PYTHONPATH=str(APP))
    subprocess.Popen([str(PYTHONW), "-m", "open_image"], env=env, cwd=str(ROOT), creationflags=NO_WINDOW)


class Setup:
    def __init__(self, log):
        self.log = log
        self.uv = BUNDLE / "uv.exe"
        self.env = dict(os.environ, UV_CACHE_DIR=str(ROOT / "cache"), UV_PYTHON_INSTALL_DIR=str(ROOT / "python"),
                        UV_NO_PROGRESS="1", UV_LINK_MODE="copy", VIRTUAL_ENV=str(VENV))

    def run(self, *args):
        self.log("$ uv " + " ".join(a for a in args if not a.startswith("--python=")))
        proc = subprocess.Popen([str(self.uv), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=self.env, creationflags=NO_WINDOW)
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self.log(line)
        if proc.wait():
            raise RuntimeError(f"uv {args[0]} failed (exit code {proc.returncode}). The log above says why.")

    def check_gpu(self):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True,
                                 text=True, timeout=20, creationflags=NO_WINDOW).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        if not out:
            raise RuntimeError("No NVIDIA graphics card was found. Open Image needs an NVIDIA GPU with up-to-date drivers.")
        self.log(f"Graphics card: {out.splitlines()[0]}")

    def install(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.check_gpu()
        self.log("Step 1 of 4: getting Python")
        if not PYTHON.exists():
            self.run("venv", str(VENV), "--python", "3.13", "--seed")
        self.log("Step 2 of 4: getting PyTorch with CUDA (about 3 GB, this is the long one)")
        lines = requirements().splitlines()
        torch_lines = [line for line in lines if line.startswith(("torch==", "torchvision=="))]
        reqs = ROOT / "requirements.txt"
        reqs.write_text("\n".join(line for line in lines if line not in torch_lines) + "\n", encoding="utf-8")
        self.run("pip", "install", f"--python={PYTHON}", *torch_lines, "--index-url", TORCH_INDEX)
        self.log("Step 3 of 4: getting the image libraries")
        self.run("pip", "install", f"--python={PYTHON}", "-r", str(reqs))
        self.log("Step 4 of 4: installing Open Image " + VERSION)
        shutil.rmtree(APP, ignore_errors=True)
        shutil.copytree(BUNDLE / "app", APP)
        self.shortcuts()
        shutil.rmtree(ROOT / "cache", ignore_errors=True)  # several GB of downloads that are no longer needed
        MARKER.write_text(json.dumps(wanted()), encoding="utf-8")
        self.log("Done.")

    def shortcuts(self):
        exe = ROOT / "OpenImage.exe"
        if getattr(sys, "frozen", False) and Path(sys.executable).resolve() != exe.resolve():
            shutil.copy2(sys.executable, exe)
        if os.environ.get("OPEN_IMAGE_NO_SHORTCUTS"):
            return
        places = [Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs", Path.home() / "Desktop"]
        script = "$s = New-Object -ComObject WScript.Shell; " + "; ".join(
            f"$l = $s.CreateShortcut('{p / 'Open Image.lnk'}'); $l.TargetPath = '{exe}'; $l.WorkingDirectory = '{ROOT}'; "
            f"$l.IconLocation = '{exe},0'; $l.Description = 'Local image generation'; $l.Save()" for p in places if p.exists())
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], creationflags=NO_WINDOW, timeout=60)


def window():
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Open Image setup")
    root.geometry("640x420")
    root.minsize(520, 340)
    root.configure(bg="#0b0c12")
    try:
        root.iconbitmap(str(BUNDLE / "icon.ico"))
    except tk.TclError:
        pass
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TProgressbar", troughcolor="#1a1d27", background="#7c6cff", bordercolor="#1a1d27", lightcolor="#7c6cff", darkcolor="#7c6cff")
    tk.Label(root, text=f"Setting up Open Image {VERSION}", fg="#eef0f7", bg="#0b0c12", font=("Segoe UI Semibold", 15), anchor="w").pack(fill="x", padx=20, pady=(18, 2))
    status = tk.Label(root, text="This happens once. It downloads about 4 GB and can take a while.", fg="#9ba2b4", bg="#0b0c12",
                      font=("Segoe UI", 10), anchor="w", justify="left")
    status.pack(fill="x", padx=20)
    bar = ttk.Progressbar(root, mode="indeterminate")
    bar.pack(fill="x", padx=20, pady=12)
    bar.start(12)
    text = tk.Text(root, bg="#12141b", fg="#aab1c2", insertbackground="#aab1c2", relief="flat", font=("Cascadia Mono", 9), wrap="word", height=12)
    text.pack(fill="both", expand=True, padx=20, pady=(0, 10))
    buttons = tk.Frame(root, bg="#0b0c12")
    buttons.pack(fill="x", padx=20, pady=(0, 16))
    lines = queue.Queue()
    result = {}

    ROOT.mkdir(parents=True, exist_ok=True)
    logfile = open(ROOT / "setup.log", "a", encoding="utf-8", buffering=1)

    def log(line):
        lines.put(line)
        logfile.write(line + "\n")

    def work():
        try:
            Setup(log).install()
            result["ok"] = True
        except Exception as err:
            log("".join(traceback.format_exception_only(type(err), err)).strip())
            result["error"] = str(err)

    def pump():
        while not lines.empty():
            line = lines.get()
            text.insert("end", line + "\n")
            text.see("end")
            if line.startswith("Step "):
                status.config(text=line)
        if "ok" in result:
            bar.stop()
            root.destroy()
            launch()
            return
        if "error" in result:
            bar.stop()
            status.config(text="Setup stopped: " + result["error"], fg="#ff8a95")
            tk.Button(buttons, text="Close", command=root.destroy, bg="#1e2230", fg="#eef0f7", relief="flat", padx=14, pady=4).pack(side="right")
            return
        root.after(150, pump)

    threading.Thread(target=work, daemon=True).start()
    pump()
    root.mainloop()


def main():
    if ready():
        launch()
    else:
        window()


if __name__ == "__main__":
    main()
