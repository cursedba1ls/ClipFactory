"""Frame-accurate planning of consecutive episodes covering the whole main video.

All boundaries are expressed in source frame indices, so consecutive episodes
share exact edges: episode k covers frames [start_frame, end_frame) and the next
one starts at exactly end_frame. Nothing is skipped, duplicated or reordered.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from fractions import Fraction

from backend.errors import UserError

DEFAULT_MIN_LAST_SECONDS = 3.0


@dataclass(frozen=True)
class EpisodeSpan:
    index: int  # 0-based
    start_frame: int
    end_frame: int  # exclusive
    start_time: float  # seconds on the normalised source timeline
    end_time: float
    duration: float

    @property
    def frames(self) -> int:
        return self.end_frame - self.start_frame

    def to_dict(self) -> dict:
        d = asdict(self)
        d["frames"] = self.frames
        return d


def frames_for_seconds(seconds: float, fps: Fraction) -> int:
    return max(1, int(round(Fraction(seconds).limit_denominator(1000) * fps)))


def plan_episodes(
    total_frames: int,
    fps: Fraction,
    episode_seconds: float,
    min_last_seconds: float = DEFAULT_MIN_LAST_SECONDS,
) -> list[EpisodeSpan]:
    """Split [0, total_frames) into consecutive episodes of ~episode_seconds.

    The final episode holds the remainder. A remainder shorter than
    min_last_seconds is appended to the previous episode instead of producing a
    uselessly tiny clip (content is still fully preserved).
    """
    if total_frames <= 0:
        raise UserError("Видео не содержит кадров.")
    if episode_seconds < 5:
        raise UserError("Длительность части должна быть не меньше 5 секунд.")
    if episode_seconds > 600:
        raise UserError("Длительность части не может превышать 10 минут.")
    fps = Fraction(fps)
    per = frames_for_seconds(episode_seconds, fps)
    count = math.ceil(total_frames / per)
    edges = [min(i * per, total_frames) for i in range(count)] + [total_frames]
    if count > 1:
        last_len = edges[-1] - edges[-2]
        if last_len / fps < min_last_seconds:
            edges.pop(-2)
    spans: list[EpisodeSpan] = []
    for i in range(len(edges) - 1):
        s, e = edges[i], edges[i + 1]
        st = float(Fraction(s) / fps)
        et = float(Fraction(e) / fps)
        spans.append(EpisodeSpan(i, s, e, st, et, et - st))
    return spans


def validate_coverage(spans: list[EpisodeSpan], total_frames: int) -> None:
    """Assert the spans tile [0, total_frames) exactly — no gaps, no overlaps, in order."""
    if not spans:
        raise AssertionError("no spans")
    if spans[0].start_frame != 0:
        raise AssertionError("first span does not start at frame 0")
    for a, b in zip(spans, spans[1:]):
        if a.end_frame != b.start_frame:
            raise AssertionError(f"gap/overlap between episode {a.index} and {b.index}")
        if b.index != a.index + 1:
            raise AssertionError("episodes out of order")
    if spans[-1].end_frame != total_frames:
        raise AssertionError("last span does not reach the end of the video")
    if any(s.frames <= 0 for s in spans):
        raise AssertionError("empty span")


@dataclass(frozen=True)
class CutWindow:
    """ffmpeg arguments describing how to extract one episode from the source.

    seek: input seek (-ss) placed before the episode start (accurate seek).
    video_trim_start/end: trim window relative to the seek point, offset by half
        a frame so frame selection is unambiguous despite float rounding.
    audio_start/end: exact audio window relative to the seek point.
    """
    seek: float
    video_trim_start: float
    video_trim_end: float
    audio_start: float
    audio_end: float
    duration: float


def cut_window(span: EpisodeSpan, fps: Fraction, t0: float = 0.0, preroll: float = 1.0) -> CutWindow:
    """Compute the ffmpeg cut window for a span.

    t0 is the offset of the first video frame relative to the file start
    (video start_time - format start_time); it accounts for containers where
    the video stream does not begin at zero.
    """
    fps = Fraction(fps)
    half = float(Fraction(1, 2) / fps)
    begin = t0 + span.start_time  # file-relative time of first frame
    seek = max(0.0, begin - preroll)
    rel = begin - seek
    v_start = max(0.0, rel - half)
    v_end = rel - half + span.duration
    return CutWindow(
        seek=seek,
        video_trim_start=v_start,
        video_trim_end=v_end,
        audio_start=rel,
        audio_end=rel + span.duration,
        duration=span.duration,
    )


def format_timecode(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
