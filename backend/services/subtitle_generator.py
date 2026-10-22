"""Speech recognition (faster-whisper), subtitle cue building, per-episode remapping and ASS/SRT output.

The main video is transcribed exactly once (cached on disk). Word timestamps on
the source timeline are then distributed to episodes and shifted to each
episode's local timeline. A cue never starts before its first word is spoken.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from pathlib import Path
from typing import Callable

from backend import config
from backend.errors import Cancelled, UserError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Styles and presets
# ---------------------------------------------------------------------------

PRESETS: dict[str, dict] = {
    # Popular TikTok look: 1–2 huge heavy words with a very thick outline, right on the split line.
    "bigword": {
        "preset": "bigword", "font_family": "Arial Black", "font_size": 124, "bold": True,
        "text_color": "#FFFFFF", "outline_color": "#000000", "outline": 10, "shadow": 0,
        "highlight_color": "#FFE600", "animation": 0.25, "uppercase": False,
        "max_words": 2, "max_line_chars": 12,
    },
    "classic": {
        "preset": "classic", "font_family": "Arial", "font_size": 82, "bold": True,
        "text_color": "#FFFFFF", "outline_color": "#000000", "outline": 7, "shadow": 0,
        "highlight_color": "#FFE600", "animation": 0.0, "uppercase": False,
    },
    "karaoke": {
        "preset": "karaoke", "font_family": "Arial", "font_size": 82, "bold": True,
        "text_color": "#FFFFFF", "outline_color": "#000000", "outline": 7, "shadow": 0,
        "highlight_color": "#FFE600", "animation": 0.3, "uppercase": False,
    },
    "minimal": {
        "preset": "minimal", "font_family": "Arial", "font_size": 64, "bold": False,
        "text_color": "#FFFFFF", "outline_color": "#000000", "outline": 2.5, "shadow": 1,
        "highlight_color": "#FFE600", "animation": 0.0, "uppercase": False,
    },
    "animated": {
        "preset": "animated", "font_family": "Arial", "font_size": 84, "bold": True,
        "text_color": "#FFFFFF", "outline_color": "#000000", "outline": 7, "shadow": 0,
        "highlight_color": "#FFE600", "animation": 0.6, "uppercase": False,
    },
}

PRESET_NAMES = {
    "bigword": "Крупно по слову",
    "classic": "TikTok Classic",
    "karaoke": "Караоке",
    "minimal": "Минимализм",
    "animated": "Анимация",
}

COMMON_STYLE = {
    "position": None,  # percent of frame height for the subtitle centre; None = layout boundary
    "max_words": 4,
    "max_line_chars": 18,
}


def resolve_style(style: dict | None) -> dict:
    style = dict(style or {})
    preset = style.get("preset") or "bigword"
    if preset not in PRESETS:
        preset = "bigword"
    out = dict(COMMON_STYLE)
    out.update(PRESETS[preset])
    for k, v in style.items():
        if k == "position" or (v is not None and k in out):
            out[k] = v
    out["preset"] = preset
    out["font_size"] = int(max(24, min(200, float(out["font_size"]))))
    out["outline"] = float(max(0.0, min(20.0, float(out["outline"]))))
    out["shadow"] = float(max(0.0, min(10.0, float(out.get("shadow") or 0))))
    out["animation"] = float(max(0.0, min(1.0, float(out.get("animation") or 0))))
    out["max_words"] = int(max(1, min(8, int(out["max_words"]))))
    out["max_line_chars"] = int(max(8, min(40, int(out["max_line_chars"]))))
    if out.get("position") is not None:
        out["position"] = float(max(5.0, min(95.0, float(out["position"]))))
    out["font_family"] = re.sub(r"[,\r\n{}\\]", "", str(out.get("font_family") or "Arial"))[:64] or "Arial"
    return out


DEFAULT_NUMBERING = {
    "enabled": True,
    "format": "Часть {n}/{total}",
    "position": "top-center",
    "size": 54,
    "margin": 70,
}

LABEL_POSITIONS = ("top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right")


def resolve_numbering(numbering: dict | None) -> dict:
    out = dict(DEFAULT_NUMBERING)
    for k, v in (numbering or {}).items():
        if k in out and v is not None:
            out[k] = v
    out["enabled"] = bool(out["enabled"])
    if out["position"] not in LABEL_POSITIONS:
        out["position"] = "top-center"
    out["size"] = int(max(20, min(140, int(out["size"]))))
    out["margin"] = int(max(10, min(400, int(out["margin"]))))
    fmt = str(out["format"] or DEFAULT_NUMBERING["format"])[:60]
    if "{n}" not in fmt:
        fmt = DEFAULT_NUMBERING["format"]
    out["format"] = fmt
    return out


def label_text(numbering: dict, n: int, total: int) -> str:
    return numbering["format"].replace("{n}", str(n)).replace("{total}", str(total))


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

_model_cache: dict[tuple, object] = {}
_model_lock = threading.Lock()


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def file_fingerprint(path: str | Path) -> str:
    p = Path(path)
    h = hashlib.sha1()
    size = p.stat().st_size
    h.update(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(1 << 20))
        if size > (2 << 20):
            f.seek(-(1 << 20), 2)
            h.update(f.read(1 << 20))
    return h.hexdigest()


def transcript_cache_path(media_path: str | Path, model: str, language: str | None) -> Path:
    key = hashlib.sha1(f"{file_fingerprint(media_path)}|{model}|{language or 'auto'}".encode()).hexdigest()
    return config.DIRS["subtitles"] / "cache" / f"{key}.json"


def _load_model(model_size: str, device: str):
    from faster_whisper import WhisperModel

    if device not in ("auto", "cpu", "cuda"):
        device = "auto"
    compute_type = {"cpu": "int8", "cuda": "float16"}.get(device, "default")
    key = (model_size, device, compute_type)
    with _model_lock:
        if key in _model_cache:
            return _model_cache[key]
        try:
            model = WhisperModel(model_size, device=device, compute_type=compute_type,
                                 download_root=str(config.DIRS["models"]))
        except Exception as exc:
            msg = str(exc)
            if device != "cpu" and ("CUDA" in msg or "cuda" in msg or "cublas" in msg.lower()):
                log.warning("CUDA unavailable for whisper, falling back to CPU: %s", msg)
                model = WhisperModel(model_size, device="cpu", compute_type="int8",
                                     download_root=str(config.DIRS["models"]))
            else:
                raise UserError(
                    f"Не удалось загрузить модель распознавания речи «{model_size}». "
                    "При первом запуске модель скачивается из интернета (один раз), "
                    "проверьте подключение или выберите другую модель в настройках.",
                    detail=msg,
                ) from exc
        _model_cache[key] = model
        return model


SKIP_STEP = 1.0          # seconds to move past a failing spot when recognition breaks there
MAX_RECOVERIES = 40      # hard limit so a broken file cannot loop forever

_CUDA_MARKERS = ("cuda", "cublas", "cudnn", "not found or cannot be loaded")
_VAD_MARKERS = ("onnx", "silero", "vad")


def _describe(exc: BaseException) -> str:
    """Never empty: type name plus message (or repr) so the user can see what broke."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else f"{type(exc).__name__}: {exc!r}"


def _valid_word(w) -> dict | None:
    """Convert one recognised word; None when it is unusable (skipped silently)."""
    try:
        text = (w.word or "").strip()
        start, end = float(w.start), float(w.end)
    except (AttributeError, TypeError, ValueError):
        return None
    if not text or not (math.isfinite(start) and math.isfinite(end)):
        return None
    if end < start:
        end = start
    try:
        prob = float(getattr(w, "probability", 1.0) or 0)
    except (TypeError, ValueError):
        prob = 0.0
    return {"start": round(start, 3), "end": round(end, 3), "text": text,
            "p": round(prob, 3) if math.isfinite(prob) else 0.0}


SAMPLE_RATE = 16000


def load_audio(audio_path: str | Path):
    """Speech audio as float32 mono 16 kHz samples (what Whisper expects).

    The pipeline extracts a 16 kHz mono 16-bit WAV with ffmpeg, which is read here with the
    standard library. Passing samples to faster-whisper (instead of a file path) means its own
    PyAV-based decoder is never used: some PyAV releases are incompatible with it
    ("open() got an unexpected keyword argument 'metadata_errors'").
    Other formats are converted by ffmpeg into raw samples.
    """
    import wave

    import numpy as np

    path = Path(audio_path)
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getframerate() == SAMPLE_RATE and wf.getsampwidth() == 2 and wf.getnchannels() in (1, 2):
                channels = wf.getnchannels()
                data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
                if channels == 2:
                    data = data[: len(data) // 2 * 2].reshape(-1, 2).mean(axis=1)
                return data.astype(np.float32) / 32768.0
    except (wave.Error, EOFError):
        pass  # not a plain PCM WAV: let ffmpeg decode it
    import subprocess

    from backend.services import ffmpeg_utils

    cmd = [ffmpeg_utils.ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(cmd, capture_output=True, creationflags=flags)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip()[-800:]
        raise UserError("Не удалось прочитать звуковую дорожку для распознавания речи.", detail=tail)
    return np.frombuffer(proc.stdout, dtype="<f4").copy()


def transcribe(
    audio_path: str | Path,
    model_size: str = "small",
    language: str | None = None,
    device: str = "auto",
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
    duration: float | None = None,
) -> dict:
    """Transcribe with faster-whisper; returns {"language", "words", "segments", "skipped"}.

    Recognition is resilient: when the engine fails while recognising some part of the audio,
    that part is skipped (listed in "skipped") and recognition continues after it. An error
    that happens before recognition starts (it would repeat on every part) is not retried
    part by part: after the GPU→CPU / VAD-off fallbacks it is reported right away with details.
    """
    if not whisper_available():
        raise UserError("Модуль распознавания речи faster-whisper не установлен. Переустановите ClipFactory (данные сохранятся).")
    audio = load_audio(audio_path)
    if not len(audio):
        raise UserError("Ошибка распознавания речи.", detail="Звуковая дорожка пустая.")
    model = _load_model(model_size, device)
    current_device = device
    lang = language if language in ("ru", "en") else None
    total = float(len(audio)) / SAMPLE_RATE
    if duration:
        total = min(total, float(duration)) if total else float(duration)
    words: list[dict] = []
    segments: list[dict] = []
    skipped: list[dict] = []
    detected = {"language": lang, "probability": 0.0}
    pos = 0.0
    use_vad = True
    recoveries = 0
    last_error: str | None = None

    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        kwargs = dict(language=lang, word_timestamps=True, vad_filter=use_vad,
                      beam_size=5, condition_on_previous_text=False)
        if use_vad:
            kwargs["vad_parameters"] = {"min_silence_duration_ms": 500}
        if pos > 0:
            kwargs["clip_timestamps"] = [round(pos, 3), round(total, 3)]
        started = False  # True once the engine accepted the audio and began yielding segments
        try:
            seg_iter, info = model.transcribe(audio, **kwargs)
            started = True
            if lang is None and getattr(info, "language", None):
                detected = {"language": getattr(info, "language", None),
                            "probability": float(getattr(info, "language_probability", 0) or 0)}
            for seg in seg_iter:
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled()
                seg_words = [w for w in (_valid_word(x) for x in (seg.words or [])) if w]
                text = (seg.text or "").strip()
                if not seg_words and text:
                    seg_words = interpolate_words(text, float(seg.start), float(seg.end))
                words.extend(seg_words)
                segments.append({"start": round(float(seg.start), 3), "end": round(float(seg.end), 3),
                                 "text": text})
                pos = max(pos, float(seg.end))
                if on_progress and total:
                    on_progress(min(1.0, pos / total))
            break  # the whole file was processed
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - the engine raises many library-specific types
            recoveries += 1
            last_error = _describe(exc)
            log.warning("recognition failed at %.1f s (attempt %d, %s): %s", pos, recoveries,
                        "during recognition" if started else "before start", last_error, exc_info=True)
            msg = last_error.lower()
            if recoveries > MAX_RECOVERIES:
                skipped.append({"start": round(pos, 2), "end": round(total, 2), "error": last_error})
                break
            if current_device != "cpu" and any(m in msg for m in _CUDA_MARKERS):
                log.warning("GPU failed during recognition, continuing on CPU")
                model = _load_model(model_size, "cpu")
                current_device = "cpu"
                continue
            if use_vad and any(m in msg for m in _VAD_MARKERS):
                use_vad = False  # voice-activity detection broke: recognise without it
                continue
            if not started:
                # The engine could not even start: the same error would repeat on every part.
                if not words:
                    raise UserError("Ошибка распознавания речи.", detail=last_error) from exc
                skipped.append({"start": round(pos, 2), "end": round(total, 2), "error": last_error})
                break
            start = pos
            pos = pos + SKIP_STEP
            if total and pos >= total:
                skipped.append({"start": round(start, 2), "end": round(total, 2), "error": last_error})
                break
            skipped.append({"start": round(start, 2), "end": round(pos, 2), "error": last_error})

    if not words and last_error and skipped:
        raise UserError(
            "Ошибка распознавания речи.",
            detail=f"Речь не распознана ни в одном участке. Последняя ошибка: {last_error}",
        )
    if total:  # the engine sometimes "hears" words past the end of the audio: drop or trim them
        words = [{**w, "end": min(w["end"], round(total, 3))} for w in words if w["start"] < total]
        segments = [{**s, "end": min(s["end"], round(total, 3))} for s in segments if s["start"] < total]
    words.sort(key=lambda w: (w["start"], w["end"]))
    return {
        "language": detected["language"],
        "language_probability": round(detected["probability"], 3),
        "model": model_size,
        "words": words,
        "segments": segments,
        "skipped": skipped,
    }


def interpolate_words(text: str, start: float, end: float) -> list[dict]:
    """Fallback when word timestamps are missing: distribute by character length."""
    tokens = text.split()
    if not tokens:
        return []
    total_chars = sum(len(t) for t in tokens) or 1
    span = max(0.01, end - start)
    out, pos = [], start
    for t in tokens:
        d = span * len(t) / total_chars
        out.append({"start": round(pos, 3), "end": round(pos + d, 3), "text": t, "p": 0.0})
        pos += d
    return out


# ---------------------------------------------------------------------------
# Cues
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?…]+[\"»”')]*$")
_CLAUSE_END = re.compile(r"[,;:—–-]+[\"»”')]*$")


def build_cues(words: list[dict], max_words: int = 4, max_chars: int = 36,
               max_gap: float = 0.6, limit: float | None = None,
               hold: float = 0.4, min_duration: float = 0.5) -> list[dict]:
    """Group words into short subtitle cues (2–5 words typical).

    Cue start equals its first word's start, so text never appears before speech.
    The end may be held slightly longer for readability, but never past the
    next cue's start or the episode end (limit).
    """
    words = [w for w in words if w.get("text", "").strip()]
    words.sort(key=lambda w: w["start"])
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        if cur:
            chars = len(" ".join(x["text"] for x in cur + [w]))
            gap = w["start"] - cur[-1]["end"]
            if len(cur) >= max_words or chars > max_chars or gap > max_gap:
                groups.append(cur)
                cur = []
        cur.append(w)
        txt = w["text"]
        if _SENTENCE_END.search(txt) or (_CLAUSE_END.search(txt) and len(cur) >= 2):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    # Merge single-word orphans into the previous cue when they are adjacent in time.
    merged: list[list[dict]] = []
    merge_limit = max_words + 1 if max_words >= 3 else max_words  # 1–2 word styles stay short
    for g in groups:
        if (merged and len(g) == 1 and len(merged[-1]) < merge_limit
                and g[0]["start"] - merged[-1][-1]["end"] < 0.25
                and not _SENTENCE_END.search(merged[-1][-1]["text"])
                and len(" ".join(x["text"] for x in merged[-1] + g)) <= max_chars):
            merged[-1] = merged[-1] + g
        else:
            merged.append(g)

    cues: list[dict] = []
    for g in merged:
        start = g[0]["start"]
        end = max(g[-1]["end"], start + 0.05)
        cues.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": " ".join(x["text"] for x in g),
            "words": [{"start": round(x["start"], 3), "end": round(x["end"], 3), "text": x["text"]} for x in g],
        })
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else None
        spoken_end = c["end"] if nxt is None else min(c["end"], nxt)
        target = max(spoken_end + hold, c["start"] + min_duration)
        if nxt is not None:
            target = min(target, nxt)
        end = max(spoken_end, target)
        if limit is not None:
            end = min(end, limit)
        c["end"] = round(end, 3)
    return [c for c in cues if c["end"] > c["start"]]


def words_for_episode(words: list[dict], start: float, end: float,
                      is_first: bool, is_last: bool) -> list[dict]:
    """Select words belonging to the episode [start, end) and shift them to local time.

    A word crossing a boundary belongs to the episode containing most of it
    (its midpoint); its display time is clipped to that episode.
    """
    out = []
    for w in words:
        ws, we = float(w["start"]), max(float(w["end"]), float(w["start"]) + 0.01)
        if we <= start or ws >= end:
            continue
        mid = (ws + we) / 2
        if mid < start and not is_first:
            continue
        if mid >= end and not is_last:
            continue
        ls = max(ws, start) - start
        le = min(we, end) - start
        if le - ls < 0.01:
            le = min(end - start, ls + 0.01)
        out.append({"start": round(ls, 3), "end": round(le, 3), "text": w["text"]})
    return out


def cues_for_episode(words: list[dict], start: float, end: float, is_first: bool, is_last: bool,
                     style: dict | None = None) -> list[dict]:
    st = resolve_style(style)
    local = words_for_episode(words, start, end, is_first, is_last)
    return build_cues(local, max_words=st["max_words"], max_chars=st["max_line_chars"] * 2,
                      limit=end - start)


def cue_words(cue: dict) -> list[dict]:
    """Words of a cue matching its (possibly edited) text, with timings."""
    tokens = str(cue.get("text", "")).split()
    words = cue.get("words") or []
    start, end = float(cue["start"]), float(cue["end"])
    if not tokens:
        return []
    if len(words) == len(tokens):
        out = []
        for t, w in zip(tokens, words):
            ws = min(max(float(w["start"]), start), end)
            we = min(max(float(w["end"]), ws), end)
            out.append({"start": ws, "end": we, "text": t})
        return out
    return [{"start": w["start"], "end": w["end"], "text": w["text"]}
            for w in interpolate_words(" ".join(tokens), start, end)]


def sanitize_cues(cues: list, duration: float) -> list[dict]:
    """Validate user-edited cues: clamp to the episode, sort, drop empties."""
    out = []
    for c in cues or []:
        try:
            s = float(c["start"])
            e = float(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = re.sub(r"\s+", " ", str(c.get("text", ""))).strip()[:300]
        if not text:
            continue
        s = max(0.0, min(s, duration))
        e = max(0.0, min(e, duration))
        if e <= s:
            continue
        words = []
        for w in c.get("words") or []:
            try:
                words.append({"start": float(w["start"]), "end": float(w["end"]), "text": str(w["text"])})
            except (KeyError, TypeError, ValueError):
                words = []
                break
        out.append({"start": round(s, 3), "end": round(e, 3), "text": text, "words": words})
    out.sort(key=lambda c: c["start"])
    return out


# ---------------------------------------------------------------------------
# ASS / SRT output
# ---------------------------------------------------------------------------

def ass_color(hex_color: str, alpha: int = 0) -> str:
    h = (hex_color or "#FFFFFF").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_inline_color(hex_color: str) -> str:
    h = (hex_color or "#FFFFFF").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = "FFFFFF"
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def ass_time(t: float, round_up: bool = False) -> str:
    cs = math.ceil(t * 100 - 1e-6) if round_up else int(round(t * 100))
    cs = max(0, cs)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{c:02d}"


def srt_time(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "∖").replace("{", "(").replace("}", ")").replace("\n", " ")


def split_lines(tokens: list[str], max_line_chars: int) -> int | None:
    """Return the index of the word starting the 2nd line, or None for a single line."""
    full = " ".join(tokens)
    if len(full) <= max_line_chars or len(tokens) < 2:
        return None
    best, best_score = None, None
    for i in range(1, len(tokens)):
        a = len(" ".join(tokens[:i]))
        b = len(" ".join(tokens[i:]))
        score = max(a, b)
        if best_score is None or score < best_score:
            best, best_score = i, score
    return best


def subtitle_block_bounds(style: dict, center_y: float) -> tuple[float, float]:
    h = 2 * style["font_size"] * 1.25 + 2 * style["outline"]
    return center_y - h / 2, center_y + h / 2


def resolve_label_position(numbering: dict, style: dict, sub_center_y: float, has_subs: bool,
                           height: int = config.OUTPUT_HEIGHT) -> str:
    """Move the episode label to the opposite edge if it would overlap the subtitles."""
    pos = numbering["position"]
    if not has_subs:
        return pos
    lh = numbering["size"] * 1.3
    m = numbering["margin"]
    top_band = (m, m + lh)
    bottom_band = (height - m - lh, height - m)
    s0, s1 = subtitle_block_bounds(style, sub_center_y)
    pad = 20

    def overlaps(band):
        return band[0] < s1 + pad and band[1] > s0 - pad

    vert, horiz = pos.split("-")
    band = top_band if vert == "top" else bottom_band
    if overlaps(band):
        other = "bottom" if vert == "top" else "top"
        other_band = bottom_band if other == "bottom" else top_band
        if not overlaps(other_band):
            return f"{other}-{horiz}"
    return pos


def build_ass(cues: list[dict], style: dict | None, numbering: dict | None,
              episode_number: int, total_episodes: int, duration: float,
              top_height: int, width: int = config.OUTPUT_WIDTH,
              height: int = config.OUTPUT_HEIGHT, banner: dict | None = None,
              banner_region: tuple[int, int] | None = None) -> str:
    """ASS file of one part. top_height is the y of the split between the pictures (subtitles default)."""
    st = resolve_style(style)
    nb = resolve_numbering(numbering)
    center_y = (st["position"] / 100.0 * height) if st.get("position") is not None else float(top_height)
    center_x = width / 2
    bold = -1 if st["bold"] else 0
    primary = ass_color(st["text_color"])
    outline_c = ass_color(st["outline_color"])
    back = ass_color(st["outline_color"], alpha=0x80)

    lines = [
        "[Script Info]",
        "; Generated by ClipFactory",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Sub,{st['font_family']},{st['font_size']},{primary},{ass_color(st['highlight_color'])},"
        f"{outline_c},{back},{bold},0,0,0,100,100,0,0,1,{st['outline']:g},{st['shadow']:g},5,40,40,0,1",
        f"Style: Label,{st['font_family']},{nb['size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{max(2.0, nb['size'] / 14):.1f},0,8,40,40,0,1",
        *_banner_style(banner),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    events: list[str] = []
    has_subs = bool(cues)
    for cue in cues:
        events.extend(_cue_events(cue, st, center_x, center_y))

    if nb["enabled"]:
        pos = resolve_label_position(nb, st, center_y, has_subs, height)
        vert, horiz = pos.split("-")
        an = {("top", "left"): 7, ("top", "center"): 8, ("top", "right"): 9,
              ("bottom", "left"): 1, ("bottom", "center"): 2, ("bottom", "right"): 3}[(vert, horiz)]
        x = {"left": 50, "center": width // 2, "right": width - 50}[horiz]
        y = nb["margin"] if vert == "top" else height - nb["margin"]
        if banner_region:
            # Keep the label off the banner strip.
            by, bh = banner_region
            lh = nb["size"] * 1.3
            y0, y1 = (y, y + lh) if vert == "top" else (y - lh, y)
            if y0 < by + bh and y1 > by:
                y = by + bh + 24 if vert == "top" else by - 24
        text = ass_escape(label_text(nb, episode_number, total_episodes))
        events.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(duration + 1)},Label,,0,0,0,,{{\\an{an}\\pos({x},{y})}}{text}"
        )
    if banner_region and (banner or {}).get("text", "").strip():
        by, bh = banner_region
        text = ass_escape(" ".join(banner["text"].split()))
        events.append(f"Dialogue: 2,{ass_time(0)},{ass_time(duration + 1)},Banner,,0,0,0,,"
                      f"{{\\an5\\q0\\pos({width // 2},{by + bh // 2})}}{text}")
    return "\n".join(lines + events) + "\n"


def _banner_style(banner: dict | None) -> list[str]:
    if not banner or not (banner.get("text") or "").strip():
        return []
    size = int(banner.get("text_size") or 72)
    color = ass_color(banner.get("text_color") or "#FFFFFF")
    return [f"Style: Banner,Arial Black,{size},{color},{color},&H00000000,&H80000000,"
            f"-1,0,0,0,100,100,0,0,1,{max(2.0, size / 16):.1f},0,5,40,40,0,1"]


def _cue_events(cue: dict, st: dict, cx: float, cy: float) -> list[str]:
    words = cue_words(cue)
    if not words:
        return []
    if st["uppercase"]:
        for w in words:
            w["text"] = w["text"].upper()
    tokens = [ass_escape(w["text"]) for w in words]
    br = split_lines([w["text"] for w in words], st["max_line_chars"])
    start, end = float(cue["start"]), float(cue["end"])
    pos = f"\\an5\\pos({cx:.0f},{cy:.0f})"
    preset = st["preset"]
    intensity = st["animation"]

    def join(parts: list[str]) -> str:
        out = []
        for i, p in enumerate(parts):
            if i:
                out.append("\\N" if i == br else " ")
            out.append(p)
        return "".join(out)

    def dialogue(s: float, e: float, body: str, layer: int = 0) -> str:
        return f"Dialogue: {layer},{ass_time(s, round_up=True)},{ass_time(e)},Sub,,0,0,0,,{{{pos}}}{body}"

    if preset in ("bigword", "classic", "minimal"):
        intro = ""
        if intensity > 0:
            sc = int(100 + 25 * intensity)
            intro = f"{{\\fscx{sc}\\fscy{sc}\\t(0,120,\\fscx100\\fscy100)}}"
        return [dialogue(start, end, intro + join(tokens))]

    hl = ass_inline_color(st["highlight_color"])
    base = ass_inline_color(st["text_color"])
    events = []
    for i, w in enumerate(words):
        ws = start if i == 0 else max(start, float(w["start"]))
        we = end if i == len(words) - 1 else max(ws + 0.01, float(words[i + 1]["start"]))
        we = min(we, end)
        if we <= ws:
            continue
        parts = []
        for j, t in enumerate(tokens):
            if preset == "karaoke":
                if j == i:
                    if intensity > 0:
                        sc = int(100 + 15 * intensity)
                        parts.append(f"{{\\c{hl}\\fscx{sc}\\fscy{sc}}}{t}{{\\c{base}\\fscx100\\fscy100}}")
                    else:
                        parts.append(f"{{\\c{hl}}}{t}{{\\c{base}}}")
                else:
                    parts.append(t)
            else:  # animated: words appear sequentially, newest pops
                if j < i:
                    parts.append(f"{{\\alpha&H00&\\fscx100\\fscy100}}{t}")
                elif j == i:
                    sc = int(100 + 40 * max(0.1, intensity))
                    dur = int(80 + 120 * max(0.1, intensity))
                    parts.append(f"{{\\alpha&H00&\\fscx{sc}\\fscy{sc}\\t(0,{dur},\\fscx100\\fscy100)}}{t}")
                else:
                    parts.append(f"{{\\alpha&HFF&\\fscx100\\fscy100}}{t}")
        events.append(dialogue(ws, we, join(parts)))
    return events


def build_srt(cues: list[dict]) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{srt_time(c['start'])} --> {srt_time(c['end'])}")
        out.append(str(c["text"]))
        out.append("")
    return "\n".join(out)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
