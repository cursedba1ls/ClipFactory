"""Media analysis with ffprobe: duration, resolution, frame rate, audio presence, VFR detection."""
from __future__ import annotations

import logging
import re
from fractions import Fraction
from pathlib import Path

from backend.errors import MediaError, UserError
from backend.services import ffmpeg_utils

log = logging.getLogger(__name__)


def parse_rate(value: str | None) -> Fraction | None:
    if not value or value in ("0/0", "0"):
        return None
    try:
        r = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return r if r > 0 else None


def _float(v, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rotation(stream: dict) -> int:
    rot = 0
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        rot = int(_float(tags["rotate"], 0) or 0)
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(_float(sd["rotation"], 0) or 0)
    return abs(rot) % 360


def count_video_packets(path: str | Path) -> int | None:
    """Exact number of video frames via demuxing (fast, no decoding)."""
    args = [
        ffmpeg_utils.ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
        "-count_packets", "-show_entries", "stream=nb_read_packets",
        "-of", "default=nokey=1:noprint_wrappers=1", str(path),
    ]
    try:
        proc = ffmpeg_utils.run_simple(args, timeout=600)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    for line in out:
        if line.strip().isdigit():
            return int(line.strip())
    return None


def analyze(path: str | Path, count_frames: bool = True) -> dict:
    """Return a JSON-serialisable description of a video file.

    Raises MediaError with a Russian message for unusable files.
    """
    p = Path(path)
    if not p.is_file():
        raise MediaError("Файл не найден.")
    if p.stat().st_size == 0:
        raise MediaError("Файл пустой.")
    try:
        data = ffmpeg_utils.ffprobe_json(p)
    except UserError as exc:
        raise MediaError(exc.message, exc.detail) from exc

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError("Файл не содержит видеодорожки.")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaError("Не удалось определить разрешение видео. Возможно, файл повреждён.")
    rotation = _rotation(video)
    if rotation in (90, 270):
        width, height = height, width

    r_rate = parse_rate(video.get("r_frame_rate"))
    avg_rate = parse_rate(video.get("avg_frame_rate"))
    fps = avg_rate or r_rate
    if fps is None:
        raise MediaError("Не удалось определить частоту кадров видео.")

    is_vfr = False
    if r_rate and avg_rate:
        diff = abs(float(r_rate) - float(avg_rate)) / float(r_rate)
        is_vfr = diff > 0.01
    if float(fps) > 240 or float(fps) < 1:
        is_vfr = True

    format_start = _float(fmt.get("start_time"), 0.0) or 0.0
    video_start = _float(video.get("start_time"), format_start)
    audio_start = _float(audio.get("start_time"), format_start) if audio else None
    video_duration = _float(video.get("duration"))
    duration = _float(fmt.get("duration")) or video_duration
    if not duration or duration <= 0:
        raise MediaError("Не удалось определить длительность видео. Возможно, файл повреждён.")
    if not video_duration:
        video_duration = duration - ((video_start or 0) - format_start)

    nb_frames = None
    if video.get("nb_frames") and str(video["nb_frames"]).isdigit():
        nb_frames = int(video["nb_frames"])
    if count_frames and (nb_frames is None or nb_frames <= 0):
        nb_frames = count_video_packets(p)

    info = {
        "path": str(p),
        "size": p.stat().st_size,
        "container": fmt.get("format_name"),
        "duration": round(duration, 6),
        "video_duration": round(video_duration, 6),
        "width": width,
        "height": height,
        "rotation": rotation,
        "video_codec": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "fps": float(fps),
        "fps_num": fps.numerator,
        "fps_den": fps.denominator,
        "r_frame_rate": video.get("r_frame_rate"),
        "avg_frame_rate": video.get("avg_frame_rate"),
        "is_vfr": is_vfr,
        "nb_frames": nb_frames,
        "format_start": format_start,
        "video_start": video_start,
        "audio_start": audio_start,
        "has_audio": audio is not None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "sample_rate": int(audio.get("sample_rate") or 0) if audio else None,
        "channels": int(audio.get("channels") or 0) if audio else None,
        "bit_rate": int(fmt.get("bit_rate") or 0) or None,
    }
    return info


_BLACK_RE = re.compile(r"black_start:(?P<s>[\d.]+)\s+black_end:(?P<e>[\d.]+)")


def detect_black_edges(path: str | Path, duration: float, window: float = 15.0) -> tuple[float, float]:
    """Return (usable_start, usable_end) excluding black frames at the very start/end.

    Used for gameplay clips so loops never flash black frames.
    """
    usable_start, usable_end = 0.0, duration

    def _scan(start: float, length: float) -> list[tuple[float, float]]:
        args = [
            ffmpeg_utils.ffmpeg_bin(), "-hide_banner", "-nostdin", "-ss", ffmpeg_utils.fmt_time(start),
            "-t", ffmpeg_utils.fmt_time(length), "-i", str(path), "-an", "-sn",
            "-vf", "blackdetect=d=0.05:pix_th=0.10", "-f", "null", "-",
        ]
        try:
            proc = ffmpeg_utils.run_simple(args, timeout=300)
        except Exception:
            return []
        text = proc.stderr.decode("utf-8", "replace")
        return [(float(m["s"]) + start, float(m["e"]) + start) for m in _BLACK_RE.finditer(text)]

    if duration <= 1.0:
        return usable_start, usable_end
    head = _scan(0.0, min(window, duration))
    for s, e in head:
        if s <= 0.1:
            usable_start = max(usable_start, e)
    tail_start = max(0.0, duration - window)
    tail = _scan(tail_start, duration - tail_start)
    for s, e in tail:
        if e >= duration - 0.15:
            usable_end = min(usable_end, s)
    if usable_end - usable_start < min(1.0, duration * 0.5):
        return 0.0, duration
    return round(usable_start, 3), round(usable_end, 3)
