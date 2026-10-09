"""PIN locks for chats and folders.

Every locked chat or folder has its own random 256-bit key. Its contents (title, prompts, settings, and the
key that seals its pictures in memory) are encrypted under that key, and the key itself is stored wrapped
twice: once under a key derived from the PIN with scrypt, and once to the master key pair if one exists.
Nothing about a locked item can be read without one of the two. Keys only exist in memory while it is open.
"""
import base64
import json
import os
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from .paths import DATA

LOCK_DIR = DATA / "locked"
MASTER_FILE = DATA / "master.json"
SCRYPT = {"n": 2**15, "r": 8, "p": 1}
MIN_PIN = 4


def b64(raw):
    return base64.b64encode(raw).decode()


def unb64(text):
    return base64.b64decode(text)


def seal(key, data, aad):
    nonce = os.urandom(12)
    return b64(nonce + AESGCM(key).encrypt(nonce, data, aad))


def unseal(key, text, aad):
    raw = unb64(text)
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], aad)
    except InvalidTag:
        return None


def stretch(pin, params):
    return Scrypt(salt=unb64(params["salt"]), length=32, n=params["n"], r=params["r"], p=params["p"]).derive(pin.encode())


def fresh_params():
    return {"salt": b64(os.urandom(16)), **SCRYPT}


def seal_to(public, data, aad):
    """Encrypt to a public key, so a key can be added to the master without the master PIN being entered."""
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(unb64(public)))
    key = HKDF(hashes.SHA256(), 32, salt=None, info=b"open-image master wrap").derive(shared)
    return b64(ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)) + "." + seal(key, data, aad)


def unseal_from(private, text, aad):
    try:
        eph, body = text.split(".", 1)
        shared = private.exchange(X25519PublicKey.from_public_bytes(unb64(eph)))
        key = HKDF(hashes.SHA256(), 32, salt=None, info=b"open-image master wrap").derive(shared)
        return unseal(key, body, aad)
    except (ValueError, InvalidTag):
        return None


# sealed documents: one per locked chat or folder

def new_doc(key, pin, master_public, payload):
    params = fresh_params()
    return {"v": 1, "kdf": params, "wrap_pin": seal(stretch(pin, params), key, b"wrap"),
            "wrap_master": seal_to(master_public, key, b"wrap") if master_public else None,
            "blob": seal(key, json.dumps(payload).encode(), b"blob")}


def open_with_pin(doc, pin):
    return unseal(stretch(pin, doc["kdf"]), doc["wrap_pin"], b"wrap")


def open_with_master(doc, private):
    return unseal_from(private, doc["wrap_master"], b"wrap") if doc.get("wrap_master") else None


def read(doc, key):
    raw = unseal(key, doc["blob"], b"blob")
    return json.loads(raw) if raw is not None else None


def write(doc, key, payload):
    doc["blob"] = seal(key, json.dumps(payload).encode(), b"blob")


def rewrap_pin(doc, key, pin):
    doc["kdf"] = params = fresh_params()
    doc["wrap_pin"] = seal(stretch(pin, params), key, b"wrap")


def add_master(doc, key, master_public):
    doc["wrap_master"] = seal_to(master_public, key, b"wrap") if master_public else None


def save_doc(item_id, doc):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LOCK_DIR / f"{item_id}.tmp"
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, LOCK_DIR / f"{item_id}.json")


def load_doc(item_id):
    try:
        return json.loads((LOCK_DIR / f"{item_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def delete_doc(item_id):
    (LOCK_DIR / f"{item_id}.json").unlink(missing_ok=True)


# the master PIN

class Master:
    """A key pair. The public half is stored in the clear; the private half is wrapped by the master PIN."""

    def __init__(self):
        self.private = None
        self.public = None
        self.params = None
        self.wrapped = None
        try:
            data = json.loads(MASTER_FILE.read_text(encoding="utf-8"))
            self.public, self.params, self.wrapped = data["public"], data["kdf"], data["private"]
        except (OSError, ValueError, KeyError):
            pass

    @property
    def exists(self):
        return self.public is not None

    @property
    def open(self):
        return self.private is not None

    def _store(self):
        MASTER_FILE.write_text(json.dumps({"public": self.public, "kdf": self.params, "private": self.wrapped}), encoding="utf-8")

    def create(self, pin):
        key = X25519PrivateKey.generate()
        raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        self.params = fresh_params()
        self.wrapped = seal(stretch(pin, self.params), raw, b"master")
        self.public = b64(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
        self.private = key
        self._store()

    def unlock(self, pin):
        raw = unseal(stretch(pin, self.params), self.wrapped, b"master")
        if raw is None:
            return False
        self.private = X25519PrivateKey.from_private_bytes(raw)
        return True

    def lock(self):
        self.private = None

    def change(self, new_pin):
        raw = self.private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        self.params = fresh_params()
        self.wrapped = seal(stretch(new_pin, self.params), raw, b"master")
        self._store()

    def remove(self):
        self.private = self.public = self.params = self.wrapped = None
        MASTER_FILE.unlink(missing_ok=True)


class Limiter:
    """Slows down guessing: after a few wrong tries every further try has to wait longer."""

    def __init__(self):
        self.fails = {}

    def wait(self, who):
        count, last = self.fails.get(who, (0, 0.0))
        if count < 3:
            return 0
        return max(0, min(2 ** (count - 2), 300) - (time.time() - last))

    def failed(self, who):
        count, _ = self.fails.get(who, (0, 0.0))
        self.fails[who] = (count + 1, time.time())

    def passed(self, who):
        self.fails.pop(who, None)


class Locked(Exception):
    pass


class BadPin(Exception):
    pass


class TooFast(Exception):
    def __init__(self, seconds):
        super().__init__(seconds)
        self.seconds = seconds


def check_pin(pin):
    if len(pin or "") < MIN_PIN:
        raise ValueError(f"Use at least {MIN_PIN} characters.")
