"""Automatic cropping.

1. Content box: black letterbox/pillarbox bars are detected (ffmpeg cropdetect) and
   removed before scaling, so "fill the frame" never keeps baked-in bars.
2. Focus: frames of a time range are sampled at low resolution; an interest map
   (edges + motion) decides where the crop window goes. Where nothing stands out
   the crop stays centred.

Crop positions are fractions 0..1 as used by the composer: x = (iw - ow) * fraction.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from backend.services import ffmpeg_utils

log = logging.getLogger(__name__)

_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")
ANALYSIS_WIDTH = 192


def detect_content_box(path: str | Path, duration: float, width: int, height: int,
                       samples: int = 8) -> dict | None:
    """Area without black bars, or None when the picture already fills the frame."""
    boxes = []
    for i in range(samples):
        t = duration * (i + 0.5) / samples
        args = [ffmpeg_utils.ffmpeg_bin(), "-hide_banner", "-nostdin", "-ss", ffmpeg_utils.fmt_time(t),
                "-i", str(path), "-frames:v", "4", "-an", "-sn",
                "-vf", "cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"]
        try:
            proc = ffmpeg_utils.run_simple(args, timeout=60)
        except Exception:
            continue
        found = _CROP_RE.findall(proc.stderr.decode("utf-8", "replace"))
        if found:
            w, h, x, y = map(int, found[-1])
            if w > 0 and h > 0:
                boxes.append((x, y, x + w, y + h))
    if not boxes:
        return None
    # Union over samples: dark scenes must not shrink the box.
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    w, h = x1 - x0, y1 - y0
    if w >= width * 0.97 and h >= height * 0.97:
        return None
    if w < width * 0.3 or h < height * 0.3:  # almost black video: do not trust
        return None
    w -= w % 2
    h -= h % 2
    return {"x": x0, "y": y0, "w": w, "h": h}


def analysis_size(src_w: int, src_h: int) -> tuple[int, int]:
    h = max(2, int(round(src_h * ANALYSIS_WIDTH / src_w / 2)) * 2)
    return ANALYSIS_WIDTH, h


def sample_frames(path: str | Path, start: float, duration: float, src_w: int, src_h: int,
                  box: dict | None = None, count: int = 16, keyframes_only: bool = False) -> np.ndarray | None:
    """Grayscale low-res frames (n, H, W) of [start, start + duration]; src_w/src_h = picture size after box."""
    duration = max(0.2, duration)
    w, h = analysis_size(src_w, src_h)
    vf = []
    if box:
        vf.append(f"crop={box['w']}:{box['h']}:{box['x']}:{box['y']}")
    vf += [f"fps={count / duration:.6f}", f"scale={w}:{h}", "format=gray"]
    # Keyframes-only decoding makes whole-video analysis fast for long sources.
    skip = ["-skip_frame", "nokey"] if keyframes_only else []
    args = [ffmpeg_utils.ffmpeg_bin(), "-v", "error", "-nostdin", *skip, "-ss", ffmpeg_utils.fmt_time(start),
            "-t", ffmpeg_utils.fmt_time(duration), "-i", str(path), "-an", "-sn",
            "-vf", ",".join(vf), "-f", "rawvideo", "-"]
    try:
        raw = ffmpeg_utils.run_simple(args, timeout=300).stdout
    except Exception:
        return None
    n = len(raw) // (w * h)
    if n == 0:
        return None
    return np.frombuffer(raw[: n * w * h], dtype=np.uint8).reshape(n, h, w).astype(np.float32)


def interest_map(frames: np.ndarray) -> np.ndarray:
    """Edges (detail) + motion, averaged over frames."""
    gx = np.abs(np.diff(frames, axis=2, prepend=frames[:, :, :1]))
    gy = np.abs(np.diff(frames, axis=1, prepend=frames[:, :1, :]))
    edges = (gx + gy).mean(axis=0)
    if len(frames) > 1:
        motion = np.abs(np.diff(frames, axis=0)).mean(axis=0)
    else:
        motion = np.zeros_like(edges)
    m = edges / (edges.mean() + 1e-6)
    if motion.any():
        m = m + 2.0 * motion / (motion.mean() + 1e-6)
    return m


def best_window(profile: np.ndarray, window: float, center_bias: float = 0.35) -> float:
    """Fraction 0..1 for the window start that captures the most interest (biased to centre)."""
    n = len(profile)
    if window >= 0.999 or n < 4:
        return 0.5
    win = max(1, int(round(window * n)))
    # Weighted sum inside each window: interest near the window centre counts more, so the
    # subject ends up centred in the crop rather than squeezed against an edge.
    kernel = 0.35 + np.hanning(win + 2)[1:-1]
    sums = np.convolve(profile, kernel[::-1], mode="valid")  # one value per start position
    if sums.max() <= 0:
        return 0.5
    starts = np.arange(len(sums))
    centre = (n - win) / 2
    bias = np.exp(-0.5 * ((starts - centre) / max(1.0, center_bias * n)) ** 2)
    score = sums * (0.85 + 0.15 * bias)
    # Flat interest (nothing stands out): stay centred.
    if sums.max() / (sums.mean() + 1e-6) < 1.12:
        return 0.5
    best = int(np.argmax(score))
    return float(best / (n - win)) if n > win else 0.5


def window_fractions(src_w: int, src_h: int, target_w: int, target_h: int, zoom: float = 1.0) -> tuple[float, float]:
    """Visible part of the source (fraction of width, fraction of height) after cover-scaling + zoom."""
    a_src = src_w / src_h
    a_dst = target_w / target_h
    wf = min(1.0, a_dst / a_src)
    hf = min(1.0, a_src / a_dst)
    zoom = max(1.0, zoom)
    return wf / zoom, hf / zoom


def focus(path: str | Path, start: float, duration: float, src_w: int, src_h: int,
          target_w: int, target_h: int, box: dict | None = None, zoom: float = 1.0,
          count: int = 16, keyframes_only: bool = False) -> dict:
    """Automatic crop position {x, y} for the time range."""
    if box:
        src_w, src_h = box["w"], box["h"]
    wf, hf = window_fractions(src_w, src_h, target_w, target_h, zoom)
    if wf >= 0.999 and hf >= 0.999:
        return {"x": 0.5, "y": 0.5}
    frames = sample_frames(path, start, duration, src_w, src_h, box, count, keyframes_only)
    if keyframes_only and (frames is None or len(frames) < 4):
        frames = sample_frames(path, start, duration, src_w, src_h, box, count)
    if frames is None:
        return {"x": 0.5, "y": 0.5}
    m = interest_map(frames)
    x = best_window(m.mean(axis=0), wf) if wf < 0.999 else 0.5
    y = best_window(m.mean(axis=1), hf) if hf < 0.999 else 0.5
    return {"x": round(x, 3), "y": round(y, 3)}


def analyze_media(path: str | Path, info: dict, kind: str, usable_start: float = 0.0,
                  usable_end: float | None = None) -> dict:
    """Crop metadata stored with a media item: content box and (for gameplay) a focus point."""
    box = detect_content_box(path, info["video_duration"], info["width"], info["height"])
    out: dict = {"content_box": box}
    if kind == "main":
        # Whole-video focus: only a preview for the crop editor; each part gets its own analysis.
        out["focus"] = focus(path, 0.0, info["video_duration"], info["width"], info["height"], 1080, 960, box,
                             count=24, keyframes_only=info["video_duration"] > 90)
    if kind == "gameplay":
        start = usable_start or 0.0
        end = usable_end or info["video_duration"]
        # Gameplay is reused across parts and layouts, so one focus point per clip (50/50 bottom half).
        out["focus"] = focus(path, start, min(60.0, max(0.5, end - start)), info["width"], info["height"],
                             1080, 960, box, count=24)
    return out
