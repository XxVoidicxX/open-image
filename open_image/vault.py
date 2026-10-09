"""In-memory encrypted store for generated pictures.

A fresh 256-bit key is made when the app starts and lives only in this process. Every picture is sealed
with AES-GCM under a random nonce and kept as ciphertext in RAM; nothing here ever touches the disk.
Pictures that belong to a locked chat or folder are sealed under that item's own key instead, which is
only in memory while the item is open. When the app closes the keys are gone and so is everything they protected.
"""
import os
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .locks import Locked

NONCE_BYTES = 12
MAX_BYTES = 2 * 2**30


class Vault:
    def __init__(self):
        self._session = AESGCM.generate_key(bit_length=256)
        self._items = {}  # (id, kind) -> (scope, sealed)
        self._size = 0
        self._lock = threading.Lock()

    @staticmethod
    def _slot(item_id, kind):
        return f"{item_id}:{kind}".encode()

    def put(self, item_id, kind, data, scope=None, key=None):
        nonce = os.urandom(NONCE_BYTES)
        sealed = nonce + AESGCM(key or self._session).encrypt(nonce, data, self._slot(item_id, kind))
        with self._lock:
            old = self._items.get((item_id, kind))
            self._size += len(sealed) - (len(old[1]) if old else 0)
            self._items[(item_id, kind)] = (scope, sealed)

    def get(self, item_id, kind, keys):
        """Decrypt a picture. `keys` maps the ids of open chats and folders to their keys."""
        with self._lock:
            entry = self._items.get((item_id, kind))
        if entry is None:
            return None
        scope, sealed = entry
        key = self._session if scope is None else keys.get(scope)
        if key is None:
            raise Locked()
        try:
            return AESGCM(key).decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], self._slot(item_id, kind))
        except InvalidTag:
            return None

    def move(self, item_id, keys, scope, key):
        """Seal an existing picture and its preview under another owner."""
        for kind in ("full", "thumb"):
            data = self.get(item_id, kind, keys)
            if data is not None:
                self.put(item_id, kind, data, scope, key)

    def scope_of(self, item_id):
        with self._lock:
            entry = self._items.get((item_id, "full"))
        return entry[0] if entry else None

    def has(self, item_id):
        with self._lock:
            return (item_id, "full") in self._items

    def drop(self, item_id):
        with self._lock:
            for kind in ("full", "thumb"):
                entry = self._items.pop((item_id, kind), None)
                if entry:
                    self._size -= len(entry[1])

    def drop_scope(self, scope):
        with self._lock:
            for slot in [s for s, (owner, _) in self._items.items() if owner == scope]:
                self._size -= len(self._items.pop(slot)[1])

    @property
    def size(self):
        return self._size

    @property
    def full(self):
        return self._size >= MAX_BYTES


vault = Vault()
