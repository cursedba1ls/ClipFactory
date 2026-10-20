"""Test configuration: isolated storage directory and synthetic media helpers."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="clipfactory-tests-"))
os.environ["CLIPFACTORY_STORAGE"] = str(_TMP / "storage")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from backend import config  # noqa: E402

MEDIA_DIR = _TMP / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_source(name: str, duration: float, fps: int = 10, size: str = "192x108", audio: bool = True,
                beeps: bool = True) -> Path:
    """Synthetic main video. Luma encodes the frame number (mod 50, step 5) so frame continuity can be checked.

    Audio: a 1 kHz beep for 0.1 s starting at every whole multiple of 7 seconds (t = 7, 14, 21 ...).
    """
    out = MEDIA_DIR / name
    if out.exists():
        return out
    vf = f"color=c=gray:s={size}:r={fps}:d={duration},geq=lum='16+mod(N\\,50)*4':cb=128:cr=128"
    args = ["-f", "lavfi", "-i", vf]
    if audio:
        expr = "0.8*sin(2*PI*1000*t)*lt(mod(t\\,7)\\,0.1)*gte(t\\,1)" if beeps else "0.1*sin(2*PI*440*t)"
        args += ["-f", "lavfi", "-i", f"aevalsrc={expr}:s=48000:d={duration}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-g", str(fps * 4), "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-b:a", "128k"]
    args += [str(out)]
    ffmpeg(*args)
    return out


def make_gameplay(name: str = "gameplay.mp4", duration: float = 20, black_head: float = 0.0) -> Path:
    out = MEDIA_DIR / name
    if out.exists():
        return out
    src = f"testsrc2=s=320x240:r=30:d={duration}"
    vf = f"drawbox=t=fill:c=black:enable='lt(t\\,{black_head})'" if black_head else "null"
    ffmpeg("-f", "lavfi", "-i", src, "-f", "lavfi", "-i", f"sine=f=300:d={duration}", "-vf", vf,
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out))
    return out


@pytest.fixture(scope="session", autouse=True)
def _storage():
    config.ensure_dirs()
    from backend.database import db
    db.init_db()
    yield
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as c:
        yield c
