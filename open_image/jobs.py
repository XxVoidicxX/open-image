import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import locks, resources, timing
from .catalog import BY_ID
from .guard import EXIT_CODE_LOW_RAM
from .library import Library
from .locks import Locked
from .paths import DATA, HOME
from .vault import vault

HISTORY_FILE = DATA / "history.json"
CHATS_FILE = DATA / "chats.json"
LOG_FILE = DATA / "worker.log"
IDLE_UNLOAD_S = 15 * 60
LIVE = ("queued", "loading", "encoding", "generating")
FINISHED = ("done", "error", "cancelled")


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Manager(Library):
    def __init__(self):
        self._init_library()
        self.jobs = {}
        self.order = []
        self.chats = {}
        self.queue = queue.Queue()
        self.lock = threading.RLock()
        self.proc = None
        self.model_id = None
        self.events = queue.Queue()
        self.busy = False
        self.last_used = time.time()
        self.cancelled = set()
        self.previews = {}  # job id -> latest preview JPEG, memory only
        self._restore()
        threading.Thread(target=self._work, daemon=True).start()
        threading.Thread(target=self._reap_idle, daemon=True).start()

    # storage

    def _restore(self):
        self._restore_library(_load(CHATS_FILE, []))
        for job in _load(HISTORY_FILE, []):
            if job["chat_id"] not in self.chats or self.chats[job["chat_id"]].get("protected"):
                continue
            if job["status"] == "done" and not vault.has(job["id"]):  # saving was off, or the picture was deleted
                job.update(image=None, expired=True)
            self.jobs[job["id"]] = job
            self.order.append(job["id"])

    def _save_jobs(self):
        done = [self.jobs[i] for i in self.order if self.jobs[i]["status"] in FINISHED and not self.is_protected_chat(self.jobs[i]["chat_id"])]
        HISTORY_FILE.write_text(json.dumps(done), encoding="utf-8")
        self._seal_open()

    def _save_chats(self):
        CHATS_FILE.write_text(json.dumps([self._chat_file_view(c) for c in self.chats.values()]), encoding="utf-8")
        self._seal_open()

    # chats

    def new_chat(self, model_id):
        with self.lock:
            now = time.time()
            chat = {"id": uuid.uuid4().hex[:10], "title": "New chat", "title_auto": True, "model": model_id,
                    "created": now, "updated": now}
            self.chats[chat["id"]] = chat
            self._save_chats()
            return chat

    def rename_chat(self, chat_id, title):
        with self.lock:
            chat = self.chats.get(chat_id)
            if not chat:
                return False
            self.require_open_chat(chat_id)
            chat["title"] = title.strip()[:80] or "Untitled chat"
            chat["title_auto"] = False
            self._save_chats()
            return True

    def delete_chat(self, chat_id):
        with self.lock:
            if chat_id not in self.chats:
                return False
            ids = [i for i in self.order if self.jobs[i]["chat_id"] == chat_id]
        for job_id in ids:
            self.cancel(job_id)
        with self.lock:
            for job_id in ids:
                self.jobs.pop(job_id, None)
                vault.drop(job_id)
            self.order = [i for i in self.order if i in self.jobs]
            if self.chats[chat_id].get("protected"):
                vault.drop_scope(chat_id)
                self.keys.pop(chat_id, None)
                self.docs.pop(chat_id, None)
                locks.delete_doc(chat_id)
            self._unfile(set(ids))
            del self.chats[chat_id]
            self._save_chats()
            self._save_jobs()
        return True

    def touch_chat(self, chat_id, model_id, prompt):
        with self.lock:
            chat = self.chats[chat_id]
            chat["updated"] = time.time()
            chat["model"] = model_id
            if chat["title_auto"] and chat["title"] == "New chat":
                text = " ".join(prompt.split())
                chat["title"] = text[:44].rsplit(" ", 1)[0] + "…" if len(text) > 46 else text
            self._save_chats()

    # worker process

    def stop_worker(self):
        with self.lock:
            proc, self.proc, self.model_id = self.proc, None, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                proc.stdin.flush()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    def _start_worker(self, model_id):
        self.stop_worker()
        self.events = events = queue.Queue()
        log = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} worker {model_id}\n")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent), OPEN_IMAGE_HOME=str(HOME), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        flags = 0x08000000 if os.name == "nt" else 0  # no console window
        proc = subprocess.Popen([sys.executable, "-m", "open_image.worker", model_id], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=log, text=True, encoding="utf-8", bufsize=1, env=env, creationflags=flags)
        self.proc, self.model_id = proc, model_id

        def read_events():
            for line in proc.stdout:
                try:
                    events.put(json.loads(line))
                except ValueError:
                    pass
            events.put({"type": "exit", "code": proc.wait()})

        threading.Thread(target=read_events, daemon=True).start()

    def _reap_idle(self):
        while True:
            time.sleep(30)
            if self.proc and not self.busy and time.time() - self.last_used > IDLE_UNLOAD_S:
                self.stop_worker()

    # queue

    def submit(self, job):
        with self.lock:
            self.jobs[job["id"]] = job
            self.order.append(job["id"])
        self.queue.put(job["id"])

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            if job["status"] == "queued":
                job["status"] = "cancelled"
                self._save_jobs()
                return True
            if job["status"] in LIVE:
                self.cancelled.add(job_id)
        self.stop_worker()
        return True

    def _work(self):
        while True:
            job = self.jobs.get(self.queue.get())
            if not job or job["status"] != "queued":
                continue
            self.busy = True
            try:
                self._run(job)
            except Exception as err:
                job.update(status="error", error=f"{type(err).__name__}: {err}")
            finally:
                self.busy = False
                self.last_used = time.time()
                self.cancelled.discard(job["id"])
                self.previews.pop(job["id"], None)
                job.pop("preview_n", None)
                with self.lock:
                    self._save_jobs()

    def _run(self, job):
        model = BY_ID[job["model"]]
        job.update(started_at=time.time(), status="loading")
        switching = self.model_id != model.id or not self.proc or self.proc.poll() is not None
        if switching:
            self.stop_worker()  # give back the previous model's memory before checking what is left
            time.sleep(1)
            if resources.read()["ram_avail_gb"] < 1.5:  # only a cold start needs fresh memory; a loaded model already has its share
                job.update(status="error", error="Your PC is almost out of free memory. Close a few programs and try again.")
                return

        if switching:
            began = time.time()
            self._start_worker(model.id)
            while True:
                event = self.events.get()
                if event["type"] == "ready":
                    if model.overhead_s == 0:
                        timing.record(model, load=time.time() - began)
                    break
                if event["type"] == "exit":
                    return self._died(job, event["code"])
        if job["id"] in self.cancelled:
            job["status"] = "cancelled"
            return

        request = {"id": job["id"], "prompt": job["final_prompt"], "negative": job["final_negative"], "width": job["width"],
                   "height": job["height"], "steps": job["steps"], "seed": job["seed"]}
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()

        while True:
            event = self.events.get()
            kind = event["type"]
            if kind == "phase":
                job["status"] = event["phase"]
                if event["phase"] == "generating":
                    job["gen_started"] = time.time()
                    job["ctx"] = timing.context()
            elif kind == "progress":
                job["step"] = event["step"]
                if job["status"] != "generating":  # a step means it is drawing, whatever phase was last announced
                    job["status"] = "generating"
                    job.setdefault("gen_started", time.time())
                    job.setdefault("ctx", timing.context())
            elif kind == "preview":
                self.previews[job["id"]] = base64.b64decode(event["data"])
                job["preview_n"] = event["step"]
            elif kind == "done":
                if "gen_started" in job:
                    megapixels = job["width"] * job["height"] / 1e6
                    prep = job["gen_started"] - job["started_at"] if model.overhead_s else None
                    timing.record(model, unit=(time.time() - job["gen_started"]) / (megapixels * job["steps"]), prep=prep, ctx=job.get("ctx"))
                scope = job["chat_id"] if self.is_protected_chat(job["chat_id"]) else None
                key = self.keys.get(scope) if scope else None
                if scope and key is None:
                    job.update(status="error", error="This chat was locked before the picture finished, so it was discarded.")
                    return
                vault.put(job["id"], "full", base64.b64decode(event["full"]), scope, key)
                vault.put(job["id"], "thumb", base64.b64decode(event["thumb"]), scope, key)
                job.update(status="done", image=f"/vault/{job['id']}.png", seconds=round(time.time() - job["started_at"], 1))
                return
            elif kind == "error":
                if job["id"] in self.cancelled:
                    job["status"] = "cancelled"
                elif event.get("kind") == "thermal":
                    job.update(status="error", error="Stopped to protect the GPU, which got too hot. Let it cool down and try again.")
                else:
                    job.update(status="error", error=event.get("message", "Something went wrong."))
                return
            elif kind == "exit":
                if job["id"] in self.cancelled:
                    job["status"] = "cancelled"
                    return
                return self._died(job, event["code"])

    def _died(self, job, code):
        self.proc = self.model_id = None
        if code == EXIT_CODE_LOW_RAM:
            error = ("The safety guard stopped this run because the PC was about to run out of memory. "
                     "Close a few programs (browsers and chat apps use a lot) and try again.")
        else:
            error = f"The model process stopped unexpectedly (exit code {code}). Details are in {LOG_FILE}."
        job.update(status="error", error=error)

    # models and images

    def has_live_jobs(self, model_id):
        with self.lock:
            return any(j["model"] == model_id and j["status"] in LIVE for j in self.jobs.values())

    def images(self, model_id=None, folder=None):
        """Pictures for the Images page. Everything shown here comes from plain chats; pictures in a locked
        chat never appear, and pictures in a locked folder only appear inside that folder."""
        with self.lock:
            if folder:
                f = self.folders.get(folder)
                if f is None:
                    raise KeyError(folder)
                if f.get("protected"):
                    if folder not in self.keys:
                        raise locks.Locked()
                    pool = list(self.folder_jobs.get(folder, {}).values())
                else:
                    pool = [self.jobs[i] for i in f["items"] if i in self.jobs]
            else:
                pool = [j for j in self.jobs.values() if not self.is_protected_chat(j["chat_id"])]
            homes = {}
            for f in self.folders.values():
                for i in f.get("items", []):
                    homes[i] = f["id"]
            done = [dict(j) for j in pool if j["status"] == "done" and vault.has(j["id"]) and (not model_id or j["model"] == model_id)]
            titles = {c["id"]: c["title"] for c in self.chats.values() if not c.get("protected")}
        done.sort(key=lambda j: j["created"], reverse=True)
        out = []
        for job in done:
            item = self.public(job)
            item["chat_title"] = titles.get(job["chat_id"], "")
            item["thumb"] = f"/vault/{job['id']}.jpg"
            item["folder"] = folder or homes.get(job["id"])
            out.append(item)
        return out

    def delete_image(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job["status"] in FINISHED:
                self.jobs.pop(job_id)
                self.order.remove(job_id)
            else:
                job = next((jobs.pop(job_id) for jobs in self.folder_jobs.values() if job_id in jobs), None)
                if not job:
                    return False
            vault.drop(job_id)
            self._unfile({job_id})
            self._save_jobs()
            self._save_folders()
        return True

    # reading state for the UI

    def remaining(self, job, res):
        """Seconds left for a queued or running job."""
        model = BY_ID[job["model"]]
        factor, _ = resources.monitor.slowdown(res, model.need_ram)
        ctx = timing.context(res)
        per_step = timing.unit_cost(model, ctx, factor) * (job["width"] * job["height"] / 1e6)
        if job["status"] == "generating":
            done = job.get("step", 0)
            if done >= 2 and job.get("gen_started"):
                per_step = (time.time() - job["gen_started"]) / done
            return max((job["steps"] - done) * per_step, 1.0) + 2
        load = (timing.load_cost(model, ctx) if self.model_id != model.id else 0) + timing.prep_cost(model, ctx, factor)
        spent = time.time() - job.get("started_at", time.time())
        return max(load - spent, 3.0) + per_step * job["steps"] + 2

    def snapshot(self, chat_id):
        res = resources.read()
        with self.lock:
            order = list(self.order)
            jobs = {i: dict(self.jobs[i]) for i in order}
            chats = [self._chat_view(c) for c in self.chats.values()]
            shut = bool(chat_id) and self.is_protected_chat(chat_id) and chat_id not in self.keys
        shown, ahead, live_count, active, counts, done_count = [], 0.0, 0, {}, {}, 0
        for job_id in order:
            job = jobs[job_id]
            counts[job["chat_id"]] = counts.get(job["chat_id"], 0) + 1
            done_count += job["status"] == "done" and vault.has(job_id) and not any(c["id"] == job["chat_id"] and c["protected"] for c in chats)
            if job["status"] in LIVE:
                job["eta_s"] = ahead + self.remaining(job, res)
                job["jobs_ahead"] = live_count
                ahead = job["eta_s"]
                live_count += 1
                active[job["chat_id"]] = active.get(job["chat_id"], 0) + 1
            if job["chat_id"] == chat_id:
                shown.append(self.public(job))
        for chat in chats:
            chat["count"] = counts.get(chat["id"], 0)
            chat["active"] = active.get(chat["id"], 0)
        chats.sort(key=lambda c: c["updated"], reverse=True)
        return {"jobs": shown, "chats": chats, "queue_ahead_s": ahead, "loaded_model": self.model_id, "busy": self.busy, "resources": res,
                "locked_chat": shut, "master": self.master_state(), "image_count": done_count, "vault_mb": round(vault.size / 2**20, 1), "vault_full": vault.full, "keep": vault.keep}

    def _chat_view(self, chat):
        view = dict(chat)
        view["protected"] = bool(chat.get("protected"))
        if view["protected"]:
            view["open"] = chat["id"] in self.keys
            if not view["open"]:
                view.update(title=chat["label"], model=None, title_auto=False)
        return view

    @staticmethod
    def public(job):
        hidden = ("final_prompt", "final_negative", "gen_started")
        out = {k: v for k, v in job.items() if k not in hidden}
        out["model_name"] = BY_ID[job["model"]].name
        return out
