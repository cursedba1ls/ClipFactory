"""Secret storage for OAuth tokens and the TikTok client secret.

On Windows secrets are encrypted with DPAPI (CryptProtectData), bound to the
current Windows user. Elsewhere they are stored in a file readable only by the
owner (chmod 600). Secrets are never written to logs.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
from pathlib import Path

from backend import config

_lock = threading.Lock()


def _store_path() -> Path:
    return config.DIRS["secrets"] / "secrets.bin"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(data: bytes) -> _Blob:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _from_blob(blob: _Blob) -> bytes:
        out = ctypes.string_at(blob.pbData, blob.cbData)
        ctypes.windll.kernel32.LocalFree(blob.pbData)
        return out

    def _protect(data: bytes) -> bytes:
        src = _to_blob(data)
        dst = _Blob()
        if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), "ClipFactory", None, None, None, 0,
                                                       ctypes.byref(dst)):
            raise OSError("CryptProtectData failed")
        return b"DPAPI" + _from_blob(dst)

    def _unprotect(data: bytes) -> bytes:
        if not data.startswith(b"DPAPI"):
            return data
        src = _to_blob(data[5:])
        dst = _Blob()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0,
                                                         ctypes.byref(dst)):
            raise OSError("CryptUnprotectData failed")
        return _from_blob(dst)
else:
    def _protect(data: bytes) -> bytes:
        return b"PLAIN" + base64.b64encode(data)

    def _unprotect(data: bytes) -> bytes:
        if data.startswith(b"PLAIN"):
            return base64.b64decode(data[5:])
        return data


def _read_all() -> dict:
    p = _store_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(_unprotect(p.read_bytes()).decode("utf-8"))
    except Exception:
        return {}


def _write_all(data: dict) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(_protect(json.dumps(data).encode("utf-8")))
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    tmp.replace(p)


def get_secret(key: str, default=None):
    with _lock:
        return _read_all().get(key, default)


def set_secret(key: str, value) -> None:
    with _lock:
        data = _read_all()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        _write_all(data)


def mask(value: str | None) -> str:
    if not value:
        return ""
    return value[:3] + "…" + value[-2:] if len(value) > 8 else "•••"
