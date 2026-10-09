"""In-memory encrypted store for generated pictures.

A fresh 256-bit key is made when the app starts and lives only in this process. Every picture is sealed
with AES-GCM under a random nonce and kept as ciphertext in RAM; nothing here ever touches the disk.
When the app closes the key is gone and so is everything it protected.
"""
import os
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
MAX_BYTES = 2 * 2**30


class Vault:
    def __init__(self):
        self._aes = AESGCM(AESGCM.generate_key(bit_length=256))
        self._items = {}
        self._size = 0
        self._lock = threading.Lock()

    @staticmethod
    def _slot(item_id, kind):
        return f"{item_id}:{kind}".encode()

    def put(self, item_id, kind, data):
        nonce = os.urandom(NONCE_BYTES)
        sealed = nonce + self._aes.encrypt(nonce, data, self._slot(item_id, kind))
        with self._lock:
            old = self._items.get((item_id, kind))
            self._size += len(sealed) - (len(old) if old else 0)
            self._items[(item_id, kind)] = sealed

    def get(self, item_id, kind):
        with self._lock:
            sealed = self._items.get((item_id, kind))
        if sealed is None:
            return None
        try:
            return self._aes.decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], self._slot(item_id, kind))
        except InvalidTag:
            return None

    def has(self, item_id):
        with self._lock:
            return (item_id, "full") in self._items

    def drop(self, item_id):
        with self._lock:
            for kind in ("full", "thumb"):
                sealed = self._items.pop((item_id, kind), None)
                if sealed:
                    self._size -= len(sealed)

    @property
    def size(self):
        return self._size

    @property
    def full(self):
        return self._size >= MAX_BYTES


vault = Vault()
