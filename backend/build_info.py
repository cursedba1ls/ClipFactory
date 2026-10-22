"""Facts fixed at build time (installer builds write build.json next to the bundled code).

Source checkouts have no build.json: they run as a development build and never install updates.
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

from backend.version import VERSION

FROZEN = bool(getattr(sys, "frozen", False))
# PyInstaller onedir: bundled data lives in sys._MEIPASS (…\_internal); the exe sits one level up.
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
INSTALL_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def info() -> dict:
    data = {
        "version": VERSION,
        "channel": "dev",
        "commit": None,
        "built_at": None,
        "update_url": None,
        "extra_public_keys": [],      # test builds only: an extra trusted update key
        "allow_insecure_updates": False,  # test builds only: http://127.0.0.1 update server
    }
    p = BUNDLE_DIR / "build.json"
    if p.is_file():
        try:
            data.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return data


def version() -> str:
    return str(info()["version"])


def is_installed_build() -> bool:
    return FROZEN and info().get("channel") != "dev"
