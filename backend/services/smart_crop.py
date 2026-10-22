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


def best_window(profile: np.ndarray, window: float, center_bias: float = 0.35, bias_weight: float = 0.15) -> float:
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
    score = sums * (1.0 - bias_weight + bias_weight * bias)
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


# ---------------------------------------------------------------------------
# Virtual camera: scene cuts, a crop window that follows the action, varied shot sizes
# ---------------------------------------------------------------------------

CAMERA_STYLES = ("static", "follow", "dynamic")
CAMERA_WIDTH = 128          # analysis width for the per-frame pass (cut detection needs every frame)
KEY_SECONDS = 2.0           # one camera key per ~2 s of a shot
DEAD_ZONE = 0.07            # ignore position changes smaller than this (fraction of the free range)
MIN_SHOT_SECONDS = 0.6
ZOOM_LEVELS = (1.0, 1.18, 1.36)
BEAT_SECONDS = (4.0, 6.5)   # "dynamic": long shots are re-framed every few seconds (punch-in / wide)


def part_frames(path: str | Path, trim_chain: str, seek: float, box: dict | None, src_w: int, src_h: int,
                width: int = CAMERA_WIDTH) -> np.ndarray | None:
    """Every frame of one part, on the same frame grid as the renderer (grayscale, low-res, uint8).

    trim_chain is the renderer's own trim/settb/setpts[/fps] chain, so frame i here is frame i of the part.
    """
    h = max(2, int(round(src_h * width / src_w / 2)) * 2)
    vf = [trim_chain]
    if box:
        vf.append(f"crop={box['w']}:{box['h']}:{box['x']}:{box['y']}")
    vf += [f"scale={width}:{h}:flags=area", "format=gray"]
    args = [ffmpeg_utils.ffmpeg_bin(), "-v", "error", "-nostdin"]
    if seek > 0:
        args += ["-ss", ffmpeg_utils.fmt_time(seek)]
    args += ["-i", str(path), "-an", "-sn", "-vf", ",".join(vf), "-f", "rawvideo", "-"]
    try:
        raw = ffmpeg_utils.run_simple(args, timeout=900).stdout
    except Exception as exc:
        log.warning("camera analysis failed: %s", exc)
        return None
    n = len(raw) // (width * h)
    if n < 2:
        return None
    return np.frombuffer(raw[: n * width * h], dtype=np.uint8).reshape(n, h, width)


def frame_differences(frames: np.ndarray) -> np.ndarray:
    """Mean absolute difference between each frame and the next (len n-1), computed in chunks."""
    out = np.empty(max(0, len(frames) - 1), dtype=np.float32)
    step = 256
    for i in range(0, len(frames) - 1, step):
        a = frames[i: i + step + 1].astype(np.int16)
        out[i: i + len(a) - 1] = np.abs(np.diff(a, axis=0)).mean(axis=(1, 2))
    return out


def detect_cuts(frames: np.ndarray, fps: float, min_shot: float = MIN_SHOT_SECONDS) -> list[int]:
    """Frame indices where a new shot starts (hard cuts): a spike well above the local motion level."""
    d = frame_differences(frames)
    n = len(frames)
    if len(d) < 3:
        return []
    min_len = max(2, int(round(min_shot * fps)))
    cuts: list[int] = []
    r = max(4, int(fps // 2))
    for i in range(len(d)):
        v = float(d[i])
        if v < 14.0:
            continue
        lo, hi = max(0, i - r), min(len(d), i + r + 1)
        local = np.concatenate([d[lo:i], d[i + 1:hi]])
        base = float(np.median(local)) if len(local) else 0.0
        if v < 3.0 * base + 6.0:
            continue
        j = i + 1  # first frame of the new shot
        if j < min_len or n - j < min_len:
            continue
        if cuts and j - cuts[-1] < min_len:
            if v > float(d[cuts[-1] - 1]):
                cuts[-1] = j
            continue
        cuts.append(j)
    return cuts


def _captured(profile: np.ndarray, frac: float) -> float:
    """Share of the interest that the best window of this size can hold."""
    if frac >= 0.999 or len(profile) < 4:
        return 1.0
    win = max(1, int(round(frac * len(profile))))
    total = float(profile.sum())
    if total <= 0:
        return 1.0
    return float(np.convolve(profile, np.ones(win), mode="valid").max() / total)


def _window_position(frames: np.ndarray, wf: float, hf: float) -> tuple[float, float]:
    m = interest_map(frames.astype(np.float32))
    x = best_window(m.mean(axis=0), wf) if wf < 0.999 else 0.5
    # Vertically, characters rarely live at the very top or bottom of a cartoon frame, while skies,
    # clouds and ground textures easily win on detail: keep a strong pull to the middle.
    y = best_window(m.mean(axis=1), hf, center_bias=0.2, bias_weight=0.6) if hf < 0.999 else 0.5
    return x, min(0.75, max(0.25, y)) if hf < 0.999 else 0.5


def _choose_zoom(frames: np.ndarray, wf: float, hf: float, base: float, prev: float | None,
                 rng) -> float:
    """Tighter shot when the action is concentrated, wide when it is spread; avoid repeating the last size."""
    m = interest_map(frames.astype(np.float32))
    px, py = m.mean(axis=0), m.mean(axis=1)
    ref = _captured(px, wf / base) * _captured(py, hf / base)
    ok = []
    for z in ZOOM_LEVELS:
        zz = min(3.0, base * z)
        cap = _captured(px, wf / zz) * _captured(py, hf / zz)
        if cap >= 0.72 * ref:
            ok.append(zz)
    if not ok:
        return base
    choices = [z for z in ok if prev is None or abs(z - prev) > 1e-3] or ok
    # Prefer the tightest acceptable shot, but keep some variety between neighbouring shots.
    if len(choices) > 1 and rng.random() < 0.35:
        return choices[-2]
    return choices[-1]


def plan_camera(frames: np.ndarray, fps: float, wf: float, hf: float, style: str = "dynamic",
                base_zoom: float = 1.0, seed: int = 0) -> list[dict]:
    """Shots covering the part: [{"f0", "f1", "zoom", "keys": [[frame, x, y], ...]}].

    follow  — a new framing at every scene cut, and the window glides after the action inside a shot;
    dynamic — the same plus varied shot sizes (wide / medium / close) and re-framing of long shots.
    """
    import random

    n = len(frames)
    base_zoom = max(1.0, min(3.0, float(base_zoom or 1.0)))
    cuts = detect_cuts(frames, fps)
    bounds = [0] + cuts + [n]
    rng = random.Random(seed)
    shots: list[tuple[int, int]] = []
    for a, b in zip(bounds, bounds[1:]):
        if style == "dynamic":
            pos = a
            while b - pos > fps * (BEAT_SECONDS[1] + 2.0):
                beat = int(round(fps * rng.uniform(*BEAT_SECONDS)))
                shots.append((pos, pos + beat))
                pos += beat
            shots.append((pos, b))
        else:
            shots.append((a, b))

    plan: list[dict] = []
    prev_zoom: float | None = None
    for a, b in shots:
        length = b - a
        step = max(1, int(round(fps / 6)))  # ~6 analysed frames per second
        sample = frames[a:b:step]
        if style == "dynamic" and length >= 1.2 * fps and len(sample) >= 3:
            zoom = _choose_zoom(sample, wf, hf, base_zoom, prev_zoom, rng)
        else:
            zoom = prev_zoom if (prev_zoom is not None and style == "dynamic") else base_zoom
        zwf, zhf = min(1.0, wf / zoom), min(1.0, hf / zoom)
        k = max(1, int(round(length / (KEY_SECONDS * fps))))
        edges = [a + int(round(length * i / k)) for i in range(k + 1)]
        values = []
        for s0, s1 in zip(edges, edges[1:]):
            sub = frames[s0:s1:step]
            if len(sub) < 2:
                sub = frames[s0:max(s1, s0 + 2)]
            x, y = _window_position(sub, zwf, zhf) if len(sub) >= 2 else (0.5, 0.5)
            values.append(((s0 + s1) // 2, x, y))
        keys: list[list] = []
        cur_x, cur_y = values[0][1], values[0][2]
        keys.append([a, round(cur_x, 3), round(cur_y, 3)])
        for f, x, y in values[1:]:
            if abs(x - cur_x) >= DEAD_ZONE:
                cur_x = x
            if abs(y - cur_y) >= DEAD_ZONE:
                cur_y = y
            if [cur_x, cur_y] != keys[-1][1:]:
                # Glide from the previous key to this one (eased in the composer).
                keys.append([f, round(cur_x, 3), round(cur_y, 3)])
        plan.append({"f0": int(a), "f1": int(b), "zoom": round(zoom, 3), "keys": keys})
        prev_zoom = zoom
    return plan
