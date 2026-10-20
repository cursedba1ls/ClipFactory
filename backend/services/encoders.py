"""H.264 encoder selection: hardware (NVENC / Quick Sync / AMF) when it really works, CPU otherwise."""
from __future__ import annotations

import logging
import threading

from backend.services import ffmpeg_utils

log = logging.getLogger(__name__)

QUALITIES = ("fast", "balanced", "high")

ENCODER_NAMES = {
    "cpu": "Процессор (libx264)",
    "nvenc": "NVIDIA NVENC",
    "qsv": "Intel Quick Sync",
    "amf": "AMD AMF",
}

_HW_CODECS = {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf"}
_detected: list[str] | None = None
_lock = threading.Lock()


def detect_hardware() -> list[str]:
    """Return working hardware encoders (probed with a tiny real encode, cached)."""
    global _detected
    with _lock:
        if _detected is not None:
            return list(_detected)
        found: list[str] = []
        try:
            ff = ffmpeg_utils.ffmpeg_bin()
            listing = ffmpeg_utils.run_simple([ff, "-hide_banner", "-encoders"], timeout=30)
            names = listing.stdout.decode("utf-8", "replace")
        except Exception:
            _detected = []
            return []
        for key, codec in _HW_CODECS.items():
            if codec not in names:
                continue
            try:
                proc = ffmpeg_utils.run_simple([
                    ff, "-hide_banner", "-nostdin", "-f", "lavfi", "-i", "color=black:s=320x240:r=30:d=0.3",
                    "-pix_fmt", "yuv420p", "-c:v", codec, "-f", "null", "-",
                ], timeout=30)
                if proc.returncode == 0:
                    found.append(key)
            except Exception:
                pass
        _detected = found
        log.info("Hardware encoders available: %s", found or "none")
        return list(found)


def resolve_encoder(preference: str) -> str:
    if preference == "cpu":
        return "cpu"
    hw = detect_hardware()
    if preference in _HW_CODECS:
        return preference if preference in hw else "cpu"
    # auto
    for key in ("nvenc", "qsv", "amf"):
        if key in hw:
            return key
    return "cpu"


def video_encoder_args(encoder: str, quality: str, fps: float) -> list[str]:
    if quality not in QUALITIES:
        quality = "balanced"
    gop = str(max(1, int(round(fps * 2))))
    if encoder == "nvenc":
        preset = {"fast": "p3", "balanced": "p5", "high": "p7"}[quality]
        cq = {"fast": "25", "balanced": "22", "high": "19"}[quality]
        args = ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr", "-cq", cq, "-b:v", "0",
                "-profile:v", "high"]
    elif encoder == "qsv":
        preset = {"fast": "veryfast", "balanced": "medium", "high": "slower"}[quality]
        gq = {"fast": "25", "balanced": "22", "high": "19"}[quality]
        args = ["-c:v", "h264_qsv", "-preset", preset, "-global_quality", gq, "-profile:v", "high"]
    elif encoder == "amf":
        q = {"fast": "speed", "balanced": "balanced", "high": "quality"}[quality]
        qp = {"fast": "25", "balanced": "22", "high": "19"}[quality]
        args = ["-c:v", "h264_amf", "-quality", q, "-rc", "cqp", "-qp_i", qp, "-qp_p", qp, "-qp_b", qp,
                "-profile:v", "high"]
    else:
        preset = {"fast": "veryfast", "balanced": "fast", "high": "slow"}[quality]
        crf = {"fast": "23", "balanced": "20", "high": "18"}[quality]
        args = ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-profile:v", "high", "-level:v", "4.2"]
    return args + ["-g", gop, "-pix_fmt", "yuv420p"]


AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
