"""The app's own key, made once on first run and kept for good.

It seals pictures from unlocked chats. On disk it is wrapped with Windows DPAPI, so the file is useless when
copied to another machine or opened by another Windows account. Elsewhere it falls back to a file only the
owner can read.
"""
import ctypes
import os

from .paths import DATA

KEY_FILE = DATA / "app.key"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    entropy = b"open-image app key"
    ebuf = ctypes.create_string_buffer(entropy, len(entropy))
    blob_entropy = _Blob(len(entropy), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = (ctypes.byref(blob_in), "Open Image" if protect else None, ctypes.byref(blob_entropy), None, None, 0x1, ctypes.byref(blob_out))
    if not fn(*args):
        raise OSError("Windows refused to " + ("protect" if protect else "unprotect") + " the app key.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def app_key():
    if KEY_FILE.exists():
        raw = KEY_FILE.read_bytes()
        if raw.startswith(b"DPAPI"):
            return _dpapi(raw[5:], protect=False)
        if raw.startswith(b"PLAIN"):
            return raw[5:]
        raise OSError(f"{KEY_FILE} is damaged. Saved pictures can't be opened without it.")
    key = os.urandom(32)
    if os.name == "nt":
        KEY_FILE.write_bytes(b"DPAPI" + _dpapi(key, protect=True))
    else:
        KEY_FILE.write_bytes(b"PLAIN" + key)
        os.chmod(KEY_FILE, 0o600)
    return key
