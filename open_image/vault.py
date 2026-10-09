"""Encrypted store for generated pictures.

Every picture and preview is sealed with AES-256-GCM before it is kept anywhere. Pictures from ordinary chats use
the app key (see keystore.py); pictures that belong to a locked chat or folder use that item's own key, which only
exists in memory while it is open. With saving on, the sealed bytes are written under data/pictures; with saving
off they stay in memory and are gone when the app closes. Either way no picture is ever written in the clear.
"""
import os
import shutil
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import keystore
from .locks import Locked
from .paths import DATA

NONCE_BYTES = 12
MAX_MEMORY_BYTES = 2 * 2**30
STORE = DATA / "pictures"
PLAIN = "_"
KINDS = ("full", "thumb")


class Vault:
    def __init__(self):
        self._app = keystore.app_key()
        self._memory = {}  # (id, kind) -> sealed bytes, when not saving
        self._scope = {}   # id -> owner (None for ordinary chats)
        self._mem_size = 0
        self._disk_size = 0
        self._lock = threading.Lock()
        self.keep = True
        self._scan()

    def _scan(self):
        if not STORE.is_dir():
            return
        for folder in STORE.iterdir():
            if not folder.is_dir():
                continue
            scope = None if folder.name == PLAIN else folder.name
            for f in folder.iterdir():
                if f.suffix == ".full":
                    self._scope[f.stem] = scope
                if f.suffix in (".full", ".thumb"):
                    self._disk_size += f.stat().st_size

    @staticmethod
    def _path(item_id, kind, scope):
        return STORE / (scope or PLAIN) / f"{item_id}.{kind}"

    @staticmethod
    def _slot(item_id, kind):
        return f"{item_id}:{kind}".encode()

    def _seal(self, item_id, kind, data, key):
        nonce = os.urandom(NONCE_BYTES)
        return nonce + AESGCM(key or self._app).encrypt(nonce, data, self._slot(item_id, kind))

    def _write(self, item_id, kind, scope, sealed):
        path = self._path(item_id, kind, scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self._disk_size -= path.stat().st_size
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(sealed)
        os.replace(tmp, path)
        self._disk_size += len(sealed)

    def _remove(self, item_id, scope):
        for kind in KINDS:
            path = self._path(item_id, kind, scope)
            try:
                self._disk_size -= path.stat().st_size
                path.unlink()
            except OSError:
                pass
            sealed = self._memory.pop((item_id, kind), None)
            if sealed:
                self._mem_size -= len(sealed)

    def put(self, item_id, kind, data, scope=None, key=None):
        sealed = self._seal(item_id, kind, data, key)
        with self._lock:
            old_scope = self._scope.get(item_id, scope)
            if old_scope != scope:
                self._remove(item_id, old_scope)
            self._scope[item_id] = scope
            if self.keep:
                self._write(item_id, kind, scope, sealed)
            else:
                old = self._memory.get((item_id, kind))
                self._mem_size += len(sealed) - (len(old) if old else 0)
                self._memory[(item_id, kind)] = sealed

    def _sealed(self, item_id, kind, scope):
        sealed = self._memory.get((item_id, kind))
        if sealed is None:
            try:
                sealed = self._path(item_id, kind, scope).read_bytes()
            except OSError:
                return None
        return sealed

    def get(self, item_id, kind, keys):
        """Decrypt a picture. `keys` maps the ids of open chats and folders to their keys."""
        with self._lock:
            if item_id not in self._scope:
                return None
            scope = self._scope[item_id]
            key = self._app if scope is None else keys.get(scope)
            if key is None:
                raise Locked()
            sealed = self._sealed(item_id, kind, scope)
        if sealed is None:
            return None
        try:
            return AESGCM(key).decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], self._slot(item_id, kind))
        except InvalidTag:
            return None

    def move(self, item_id, keys, scope, key):
        """Seal an existing picture and its preview under another owner."""
        data = {kind: self.get(item_id, kind, keys) for kind in KINDS}
        for kind, plain in data.items():
            if plain is not None:
                self.put(item_id, kind, plain, scope, key)

    def scope_of(self, item_id):
        return self._scope.get(item_id)

    def has(self, item_id):
        return item_id in self._scope

    def drop(self, item_id):
        with self._lock:
            if item_id in self._scope:
                self._remove(item_id, self._scope.pop(item_id))

    def drop_scope(self, scope):
        with self._lock:
            for item_id in [i for i, s in self._scope.items() if s == scope]:
                self._remove(item_id, scope)
                del self._scope[item_id]
            shutil.rmtree(STORE / scope, ignore_errors=True)

    def set_keep(self, keep):
        """Switch between saving sealed pictures to disk and keeping them only in memory."""
        with self._lock:
            if keep == self.keep:
                return
            self.keep = keep
            for item_id, scope in list(self._scope.items()):
                for kind in KINDS:
                    if keep:
                        sealed = self._memory.pop((item_id, kind), None)
                        if sealed:
                            self._mem_size -= len(sealed)
                            self._write(item_id, kind, scope, sealed)
                    else:
                        path = self._path(item_id, kind, scope)
                        try:
                            sealed = path.read_bytes()
                        except OSError:
                            continue
                        self._memory[(item_id, kind)] = sealed
                        self._mem_size += len(sealed)
                        self._disk_size -= len(sealed)
                        path.unlink()
            if not keep:
                shutil.rmtree(STORE, ignore_errors=True)

    @property
    def size(self):
        return self._disk_size if self.keep else self._mem_size

    @property
    def full(self):
        return not self.keep and self._mem_size >= MAX_MEMORY_BYTES


vault = Vault()
