"""Import of ready-made subtitle files (.srt / .vtt) for the main video.

When a file is attached, its text is used instead of speech recognition (handy for
cartoons that already have subtitles, or have no speech at all). Cue timings are
spread over the words so the usual 1–5-word cues can be built from them.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from backend import config
from backend.errors import UserError
from backend.services.subtitle_generator import interpolate_words, save_json

_TIME = r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_CUE_RE = re.compile(_TIME + r"\s*-->\s*" + _TIME)
_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")


def _secs(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_subtitles(text: str) -> list[dict]:
    """Parse SRT or WebVTT into [{start, end, text}] sorted by time."""
    text = text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = _CUE_RE.search(lines[i])
        if not m:
            i += 1
            continue
        g = m.groups()
        start, end = _secs(*g[0:4]), _secs(*g[4:8])
        i += 1
        body = []
        while i < len(lines) and lines[i].strip() and not _CUE_RE.search(lines[i]):
            body.append(lines[i].strip())
            i += 1
        t = html.unescape(_TAG_RE.sub("", " ".join(body)))
        t = re.sub(r"\s+", " ", t).strip()
        if t and end > start:
            cues.append({"start": round(start, 3), "end": round(end, 3), "text": t})
    cues.sort(key=lambda c: c["start"])
    return cues


def cues_to_words(cues: list[dict]) -> list[dict]:
    words = []
    for c in cues:
        words.extend(interpolate_words(c["text"], c["start"], c["end"]))
    return words


def external_path(media_id: str) -> Path:
    if not media_id.isalnum():
        raise ValueError("bad media id")
    return config.DIRS["subtitles"] / "external" / f"{media_id}.json"


def save_external(media_id: str, filename: str, raw: bytes) -> dict:
    if len(raw) > 5 * 1024 * 1024:
        raise UserError("Файл субтитров слишком большой (больше 5 МБ).")
    text = None
    for enc in ("utf-8-sig", "cp1251", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise UserError("Не удалось прочитать файл субтитров: неизвестная кодировка.")
    cues = parse_subtitles(text)
    if not cues:
        raise UserError("В файле не найдено ни одной фразы. Поддерживаются форматы .srt и .vtt.")
    data = {"source": "file", "filename": filename[:200], "language": None, "model": "file",
            "cues": cues, "words": cues_to_words(cues), "segments": []}
    save_json(external_path(media_id), data)
    return data


def load_external(media_id: str) -> dict | None:
    p = external_path(media_id)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def remove_external(media_id: str) -> None:
    external_path(media_id).unlink(missing_ok=True)
