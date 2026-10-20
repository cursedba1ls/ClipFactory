"""Safe wrappers around ffmpeg/ffprobe subprocesses.

Commands are always built as argument lists (never through a shell), so user
input can never be interpreted as shell syntax.
"""
from __future__ import annotations

import collections
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from backend import config
from backend.errors import Cancelled, UserError

log = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class FFmpegError(UserError):
    pass


def ffmpeg_bin() -> str:
    path = config.find_binary("ffmpeg")
    if not path:
        raise UserError(
            "FFmpeg не найден. Установите FFmpeg (см. README) или положите ffmpeg.exe в папку tools\\ffmpeg\\bin."
        )
    return path


def ffprobe_bin() -> str:
    path = config.find_binary("ffprobe")
    if not path:
        raise UserError(
            "ffprobe не найден. Он входит в комплект FFmpeg — установите FFmpeg полностью (см. README)."
        )
    return path


def run_simple(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )


def ffprobe_json(path: str | Path, extra: Iterable[str] = ()) -> dict:
    args = [
        ffprobe_bin(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", *extra, str(path),
    ]
    try:
        proc = run_simple(args, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise UserError("Анализ файла занял слишком много времени. Возможно, файл повреждён.") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise UserError("Файл повреждён или имеет неподдерживаемый формат.", detail=err[-2000:])
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise UserError("Не удалось прочитать информацию о файле.") from exc


class ProcessRegistry:
    """Tracks running ffmpeg processes so they can be terminated on cancel/shutdown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    def add(self, p: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(p)

    def remove(self, p: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(p)

    def kill_all(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            _terminate(p)


registry = ProcessRegistry()


def _terminate(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        if p.stdin:
            try:
                p.stdin.write(b"q")
                p.stdin.flush()
            except Exception:
                pass
        p.wait(timeout=3)
    except Exception:
        pass
    if p.poll() is None:
        p.kill()


def run_ffmpeg(
    args: list[str],
    duration: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
    cwd: str | Path | None = None,
    error_message: str = "Ошибка обработки видео в FFmpeg.",
) -> None:
    """Run ffmpeg with machine-readable progress output.

    on_progress receives a fraction 0..1 when duration is known.
    """
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-y", "-progress", "pipe:1", *args]
    log.debug("ffmpeg: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        creationflags=_CREATE_NO_WINDOW,
    )
    registry.add(proc)
    stderr_tail: collections.deque[str] = collections.deque(maxlen=60)

    def _read_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            stderr_tail.append(raw.decode("utf-8", "replace").rstrip())

    t = threading.Thread(target=_read_stderr, daemon=True)
    t.start()

    cancelled = False
    watcher_stop = threading.Event()

    def _watch_cancel() -> None:
        nonlocal cancelled
        while not watcher_stop.is_set():
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _terminate(proc)
                return
            time.sleep(0.2)

    w = threading.Thread(target=_watch_cancel, daemon=True)
    w.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not on_progress or not duration:
                continue
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                on_progress(max(0.0, min(1.0, us / 1_000_000 / duration)))
        proc.wait()
    finally:
        watcher_stop.set()
        registry.remove(proc)
        t.join(timeout=2)
    if cancelled or (cancel_event is not None and cancel_event.is_set()):
        raise Cancelled()
    if proc.returncode != 0:
        tail = "\n".join(stderr_tail)
        log.error("ffmpeg failed (%s): %s\n%s", proc.returncode, " ".join(cmd), tail)
        raise FFmpegError(error_message, detail=tail[-3000:])
    if on_progress:
        on_progress(1.0)


def fmt_time(seconds: float) -> str:
    """Format seconds for ffmpeg arguments with microsecond precision."""
    return f"{max(0.0, seconds):.6f}"
