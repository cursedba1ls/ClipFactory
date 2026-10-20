"""Paths, constants and environment configuration for ClipFactory."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) so python-dotenv is not required."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")

APP_NAME = "ClipFactory"
APP_VERSION = "1.0.0"

HOST = os.environ.get("CLIPFACTORY_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLIPFACTORY_PORT", "8765"))

STORAGE = Path(os.environ.get("CLIPFACTORY_STORAGE", str(ROOT / "storage"))).resolve()
FRONTEND_DIR = ROOT / "frontend"

DIRS = {
    "downloads": STORAGE / "downloads",
    "gameplay": STORAGE / "gameplay",
    "projects": STORAGE / "projects",
    "exports": STORAGE / "exports",
    "subtitles": STORAGE / "subtitles",
    "temp": STORAGE / "temp",
    "logs": STORAGE / "logs",
    "models": STORAGE / "models",
    "secrets": STORAGE / "secrets",
}

DB_PATH = STORAGE / "clipfactory.db"

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts"}

TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REDIRECT_URI = os.environ.get("TIKTOK_REDIRECT_URI", "")


def ensure_dirs() -> None:
    for d in DIRS.values():
        d.mkdir(parents=True, exist_ok=True)


def find_binary(name: str) -> str | None:
    """Locate ffmpeg/ffprobe: env override, bundled tools folder, then PATH."""
    env_key = f"{name.upper()}_PATH"
    candidates: list[Path] = []
    if os.environ.get(env_key):
        candidates.append(Path(os.environ[env_key]))
    exe = name + (".exe" if sys.platform == "win32" else "")
    candidates.append(ROOT / "tools" / "ffmpeg" / "bin" / exe)
    candidates.append(ROOT / "tools" / exe)
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which(name)
