"""Locked chats, folders and the master PIN, as a part of the job manager.

A chat or folder is either plain or protected. A protected one keeps only a label, an id and some
timestamps in the clear. Its title, prompts, settings and pictures can only be read while its key is in
`self.keys`, which happens after its PIN (or the master PIN) has been entered and ends when it is locked again.
"""
import json
import os
import time
import uuid

from . import locks
from .locks import BadPin, Locked, TooFast
from .paths import DATA
from .vault import vault

FOLDERS_FILE = DATA / "folders.json"
PRIVATE_CHAT_FIELDS = ("title", "title_auto", "model")
LIVE = ("queued", "loading", "encoding", "generating")


class Library:
    def _init_library(self):
        self.master = locks.Master()
        self.limiter = locks.Limiter()
        self.keys = {}
        self.docs = {}
        self.folders = {}
        self.folder_jobs = {}

    def _restore_library(self, stored_chats):
        for chat in stored_chats:
            if chat.get("protected"):
                doc = locks.load_doc(chat["id"])
                if doc is None:
                    continue
                self.docs[chat["id"]] = doc
            self.chats[chat["id"]] = chat
        try:
            stored = json.loads(FOLDERS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = []
        for folder in stored:
            if folder.get("protected"):
                doc = locks.load_doc(folder["id"])
                if doc is None:
                    continue
                self.docs[folder["id"]] = doc
            self.folders[folder["id"]] = folder

    # saving

    def _chat_file_view(self, chat):
        if chat.get("protected"):
            return {k: chat[k] for k in ("id", "protected", "label", "created", "updated")}
        return chat

    def _folder_file_view(self, folder):
        if folder.get("protected"):
            return {k: folder[k] for k in ("id", "protected", "label", "created")}
        return folder

    def _save_folders(self):
        FOLDERS_FILE.write_text(json.dumps([self._folder_file_view(f) for f in self.folders.values()]), encoding="utf-8")
        self._seal_open()

    def _payload(self, kind, cid):
        if kind == "chat":
            chat = self.chats[cid]
            jobs = [self.jobs[i] for i in self.order if self.jobs[i]["chat_id"] == cid and self.jobs[i]["status"] in ("done", "error", "cancelled")]
            return {"chat": {k: chat[k] for k in PRIVATE_CHAT_FIELDS}, "jobs": jobs}
        return {"name": self.folders[cid]["name"], "jobs": list(self.folder_jobs.get(cid, {}).values())}

    def _seal_open(self):
        """Write the current contents of every open protected chat and folder, encrypted, to disk."""
        for cid, key in list(self.keys.items()):
            kind = "chat" if cid in self.chats else "folder"
            locks.write(self.docs[cid], key, self._payload(kind, cid))
            locks.save_doc(cid, self.docs[cid])

    def _kind_of(self, kind, cid):
        item = (self.chats if kind == "chat" else self.folders).get(cid)
        if item is None:
            raise KeyError(cid)
        return item

    def is_protected_chat(self, chat_id):
        chat = self.chats.get(chat_id)
        return bool(chat and chat.get("protected"))

    def require_open_chat(self, chat_id):
        if self.is_protected_chat(chat_id) and chat_id not in self.keys:
            raise Locked()

    # locking and unlocking

    def _has_live(self, chat_id):
        return any(j["chat_id"] == chat_id and j["status"] in LIVE for j in self.jobs.values())

    def protect(self, kind, cid, pin, label):
        locks.check_pin(pin)
        with self.lock:
            item = self._kind_of(kind, cid)
            if item.get("protected"):
                raise ValueError("That is already locked.")
            if kind == "chat" and self._has_live(cid):
                raise ValueError("Wait for the running images in this chat to finish, then lock it.")
            key = os.urandom(32)
            if kind == "chat":
                ids = [i for i in self.order if self.jobs[i]["chat_id"] == cid]
                for i in ids:
                    vault.move(i, self.keys, cid, key)
                self._unfile(set(ids))
            else:
                self.folder_jobs[cid] = {}
                self.keys[cid] = key  # lets the pictures be moved in below
                for i in list(item.get("items", [])):
                    if i in self.jobs:
                        self._move_into_folder(i, cid)
                item.pop("items", None)
            doc = locks.new_doc(key, pin, self.master.public, {})
            self.docs[cid], self.keys[cid] = doc, key
            default = "Locked chat" if kind == "chat" else "Locked folder"
            item.update(protected=True, label=(label or "").strip()[:60] or default)
            self._persist(kind)

    def _persist(self, kind):
        if kind == "chat":
            self._save_chats()
            self._save_jobs()
        else:
            self._save_folders()
            self._save_chats()
            self._save_jobs()

    def unlock(self, kind, cid, pin):
        with self.lock:
            item = self._kind_of(kind, cid)
            if not item.get("protected"):
                return
            if cid in self.keys:
                return
            wait = self.limiter.wait(cid)
            if wait > 0:
                raise TooFast(wait)
            key = locks.open_with_pin(self.docs[cid], pin or "")
            if key is None:
                self.limiter.failed(cid)
                raise BadPin()
            self.limiter.passed(cid)
            self._open(kind, cid, key)

    def _open(self, kind, cid, key):
        doc = self.docs[cid]
        payload = locks.read(doc, key)
        if payload is None:
            raise BadPin()
        item = self._kind_of(kind, cid)
        self.keys[cid] = key
        jobs = payload.get("jobs", [])
        for job in jobs:
            if job["status"] == "done" and not vault.has(job["id"]):
                job.update(image=None, expired=True)
        if kind == "chat":
            item.update(payload["chat"])
            for job in sorted(jobs, key=lambda j: j["created"]):
                self.jobs[job["id"]] = job
                self.order.append(job["id"])
        else:
            item["name"] = payload["name"]
            self.folder_jobs[cid] = {j["id"]: j for j in jobs}
        if self.master.exists and not doc.get("wrap_master"):
            locks.add_master(doc, key, self.master.public)
            locks.save_doc(cid, doc)

    def relock(self, kind, cid):
        with self.lock:
            item = self._kind_of(kind, cid)
            if not item.get("protected") or cid not in self.keys:
                return
            if kind == "chat" and self._has_live(cid):
                raise ValueError("Wait for the running images in this chat to finish first.")
            self._seal_open()
            if kind == "chat":
                gone = {i for i in self.order if self.jobs[i]["chat_id"] == cid}
                for i in gone:
                    self.jobs.pop(i, None)
                self.order = [i for i in self.order if i not in gone]
                for field in PRIVATE_CHAT_FIELDS:
                    item.pop(field, None)
            else:
                self.folder_jobs.pop(cid, None)
                item.pop("name", None)
            del self.keys[cid]

    def unprotect(self, kind, cid, pin):
        with self.lock:
            item = self._kind_of(kind, cid)
            if not item.get("protected"):
                return
            if cid not in self.keys:
                raise Locked()
            if locks.open_with_pin(self.docs[cid], pin or "") is None:
                raise BadPin()
            if kind == "chat":
                for i in [i for i in self.order if self.jobs[i]["chat_id"] == cid]:
                    vault.move(i, self.keys, None, None)
            else:
                raise ValueError("Move the pictures out of a locked folder before removing its lock, or delete the folder.")
            del self.keys[cid]
            self.docs.pop(cid, None)
            locks.delete_doc(cid)
            item.pop("protected", None)
            item.pop("label", None)
            self._persist(kind)

    def repin(self, kind, cid, old, new):
        locks.check_pin(new)
        with self.lock:
            self._kind_of(kind, cid)
            if cid not in self.keys:
                raise Locked()
            if locks.open_with_pin(self.docs[cid], old or "") is None:
                raise BadPin()
            locks.rewrap_pin(self.docs[cid], self.keys[cid], new)
            locks.save_doc(cid, self.docs[cid])

    # master PIN

    def master_state(self):
        return {"set": self.master.exists, "open": self.master.open}

    def _master_guard(self, pin):
        wait = self.limiter.wait("master")
        if wait > 0:
            raise TooFast(wait)
        if not self.master.unlock(pin or ""):
            self.limiter.failed("master")
            raise BadPin()
        self.limiter.passed("master")

    def master_set(self, pin, old=""):
        locks.check_pin(pin)
        with self.lock:
            if self.master.exists:
                self._master_guard(old)
                self.master.change(pin)
                return
            self.master.create(pin)
            for cid, key in self.keys.items():
                locks.add_master(self.docs[cid], key, self.master.public)
                locks.save_doc(cid, self.docs[cid])

    def master_unlock(self, pin):
        with self.lock:
            if not self.master.exists:
                raise ValueError("There is no master PIN.")
            self._master_guard(pin)
            for cid in list(self.docs):
                if cid in self.keys:
                    continue
                key = locks.open_with_master(self.docs[cid], self.master.private)
                if key is not None:
                    self._open("chat" if cid in self.chats else "folder", cid, key)

    def master_remove(self, pin):
        with self.lock:
            if not self.master.exists:
                return
            self._master_guard(pin)
            self.master.remove()
            for cid, doc in self.docs.items():
                if doc.get("wrap_master"):
                    doc["wrap_master"] = None
                    if cid in self.keys:
                        locks.save_doc(cid, doc)

    def lock_all(self):
        """Close everything that is open. Returns how many chats could not be closed because they are still drawing."""
        busy = 0
        with self.lock:
            for cid in list(self.keys):
                kind = "chat" if cid in self.chats else "folder"
                try:
                    self.relock(kind, cid)
                except ValueError:
                    busy += 1
            self.master.lock()
        return busy

    # folders

    def _unfile(self, job_ids):
        changed = False
        for folder in self.folders.values():
            items = folder.get("items")
            if items and job_ids & set(items):
                folder["items"] = [i for i in items if i not in job_ids]
                changed = True
        if changed:
            self._save_folders()

    def new_folder(self, name):
        with self.lock:
            folder = {"id": uuid.uuid4().hex[:10], "name": (name or "").strip()[:60] or "New folder", "items": [], "created": time.time()}
            self.folders[folder["id"]] = folder
            self._save_folders()
            return folder

    def rename_folder(self, fid, name):
        with self.lock:
            folder = self._kind_of("folder", fid)
            if folder.get("protected") and fid not in self.keys:
                raise Locked()
            folder["name"] = (name or "").strip()[:60] or "Untitled folder"
            self._save_folders()

    def delete_folder(self, fid):
        with self.lock:
            folder = self._kind_of("folder", fid)
            if folder.get("protected"):
                for job_id in list(self.folder_jobs.get(fid, {})):
                    vault.drop(job_id)
                vault.drop_scope(fid)
                self.folder_jobs.pop(fid, None)
                self.keys.pop(fid, None)
                self.docs.pop(fid, None)
                locks.delete_doc(fid)
            del self.folders[fid]
            self._save_folders()

    def _move_into_folder(self, job_id, fid):
        job = self.jobs.pop(job_id)
        self.order.remove(job_id)
        vault.move(job_id, self.keys, fid, self.keys[fid])
        job["chat_id"] = None
        self.folder_jobs[fid][job_id] = job

    def add_to_folder(self, fid, job_id):
        with self.lock:
            folder = self._kind_of("folder", fid)
            job = self.jobs.get(job_id)
            if not job or job["status"] != "done" or not vault.has(job_id):
                raise KeyError(job_id)
            if folder.get("protected"):
                if fid not in self.keys:
                    raise Locked()
                self._move_into_folder(job_id, fid)
                self._save_jobs()
            else:
                if self.is_protected_chat(job["chat_id"]):
                    raise ValueError("Pictures from a locked chat can only go into a locked folder.")
                if job_id not in folder["items"]:
                    folder["items"].append(job_id)
                self._save_folders()

    def remove_from_folder(self, fid, job_id):
        with self.lock:
            folder = self._kind_of("folder", fid)
            if folder.get("protected"):
                raise ValueError("Pictures in a locked folder can only be saved or deleted.")
            folder["items"] = [i for i in folder["items"] if i != job_id]
            self._save_folders()

    def folder_list(self):
        with self.lock:
            out = []
            for f in sorted(self.folders.values(), key=lambda f: f["created"]):
                if f.get("protected"):
                    is_open = f["id"] in self.keys
                    out.append({"id": f["id"], "protected": True, "open": is_open, "label": f["label"],
                                "name": f["name"] if is_open else f["label"],
                                "count": len(self.folder_jobs.get(f["id"], {})) if is_open else None})
                else:
                    count = sum(1 for i in f["items"] if i in self.jobs and vault.has(i))
                    out.append({"id": f["id"], "protected": False, "open": True, "name": f["name"], "label": f["name"], "count": count})
            return out

    def find_job(self, job_id):
        job = self.jobs.get(job_id)
        if job:
            return job
        for jobs in self.folder_jobs.values():
            if job_id in jobs:
                return jobs[job_id]
        return None
