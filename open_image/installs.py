"""Installing and removing models from inside the app.

A download runs as its own `python -m open_image download <id>` process so it can be cancelled by killing it.
Progress comes from watching the model folder grow, measured against the size reported by Hugging Face.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .catalog import MARKER, folder_size
from .paths import DATA, HOME

GIB = 2**30
SPACE_MARGIN = 2 * GIB
LOG_FILE = DATA / "download.log"


def remote_size(model):
    from huggingface_hub import HfApi
    from huggingface_hub.utils import filter_repo_objects
    info = HfApi().model_info(model.repo, files_metadata=True)
    files = filter_repo_objects(info.siblings, allow_patterns=list(model.allow) or None,
                                ignore_patterns=list(model.ignore) or None, key=lambda f: f.rfilename)
    return sum(f.size or 0 for f in files)


class Installer:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()

    def downloading(self, model_id):
        job = self.jobs.get(model_id)
        return bool(job) and job["state"] in ("checking", "downloading")

    def start(self, model):
        with self.lock:
            if self.downloading(model.id):
                return
            self.jobs[model.id] = {"state": "checking", "total": 0, "error": "", "proc": None, "samples": [], "cancelled": False}
        threading.Thread(target=self._run, args=(model,), daemon=True).start()

    def cancel(self, model_id):
        job = self.jobs.get(model_id)
        if not job:
            return
        job["cancelled"] = True
        proc = job.get("proc")
        if proc and proc.poll() is None:
            proc.kill()

    def _fail(self, job, message):
        job.update(state="error", error=message)

    def _run(self, model):
        job = self.jobs[model.id]
        try:
            total = remote_size(model)
        except Exception:
            return self._fail(job, "Couldn't reach Hugging Face. Check your internet connection and try again.")
        job["total"] = total
        have = folder_size(model.path) if model.path.exists() else 0
        free = shutil.disk_usage(HOME).free
        if total - have + SPACE_MARGIN > free:
            return self._fail(job, f"Not enough disk space: this needs about {(total - have) / GIB:.0f} GB and only {free / GIB:.0f} GB is free.")
        (model.path / MARKER).unlink(missing_ok=True)

        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent), OPEN_IMAGE_HOME=str(HOME),
                   HF_HUB_DISABLE_XET="1", HF_HUB_DOWNLOAD_TIMEOUT="30")
        log = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} download {model.id}\n")
        flags = 0x08000000 if os.name == "nt" else 0
        proc = subprocess.Popen([sys.executable, "-m", "open_image", "download", model.id], stdout=log, stderr=log, env=env, creationflags=flags)
        job.update(proc=proc, state="downloading")
        code = proc.wait()
        if job["cancelled"]:
            job["state"] = "idle"
        elif code == 0:
            job["state"] = "done"
        else:
            self._fail(job, f"The download stopped (exit code {code}). It can be resumed. Details are in {LOG_FILE}.")

    def status(self, model):
        job = self.jobs.get(model.id)
        if not job or job["state"] == "done":
            return {"state": "idle"}
        out = {"state": job["state"], "total": job["total"], "error": job["error"]}
        if job["state"] == "downloading":
            now, got = time.time(), folder_size(model.path)
            samples = [s for s in job["samples"] if now - s[0] < 12] + [(now, got)]
            job["samples"] = samples
            speed = (got - samples[0][1]) / (now - samples[0][0]) if now - samples[0][0] > 1 else 0
            left = max(job["total"] - got, 0)
            out.update(bytes=got, speed_bps=speed, eta_s=left / speed if speed > 0 else None,
                       progress=min(got / job["total"], 0.999) if job["total"] else 0)
        return out

    def uninstall(self, model):
        self.jobs.pop(model.id, None)
        if not model.path.exists():
            return
        last = None
        for _ in range(5):  # a worker that just exited can still hold files for a moment
            try:
                shutil.rmtree(model.path)
                return
            except OSError as err:
                last = err
                time.sleep(1)
        raise last
