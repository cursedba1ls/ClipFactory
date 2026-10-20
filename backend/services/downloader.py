"""Video import: URL download via yt-dlp and safe local file handling.

Only publicly accessible media the user has rights to is supported. No DRM
circumvention, no cookies/credentials, no access-restriction bypassing.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from backend import config
from backend.errors import Cancelled, UserError

log = logging.getLogger(__name__)


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise UserError("Введите ссылку на видео.")
    if len(url) > 2048:
        raise UserError("Ссылка слишком длинная.")
    if any(c in url for c in "\r\n\t ") or any(ord(c) < 32 for c in url):
        raise UserError("Ссылка содержит недопустимые символы.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UserError("Поддерживаются только ссылки, начинающиеся с http:// или https://")
    if not parts.hostname:
        raise UserError("Некорректная ссылка: не указан адрес сайта.")
    if parts.username or parts.password:
        raise UserError("Ссылки с логином и паролем не поддерживаются.")
    host = parts.hostname.lower()
    if host in ("localhost",) or host.endswith(".local"):
        raise UserError("Локальные адреса не поддерживаются. Загрузите файл с компьютера.")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise UserError("Локальные адреса не поддерживаются. Загрузите файл с компьютера.")
    except ValueError:
        pass
    return url


def safe_filename(name: str, max_len: int = 120) -> str:
    """Display-safe filename (used only for metadata/downloads, never for storage paths)."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r"\.\.+", "", name)
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    if not name or name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)\d", name.upper()):
        name = "video"
    return name[:max_len]


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _map_ytdlp_error(text: str, max_mb: int) -> UserError:
    t = text.lower()
    if "unsupported url" in t:
        return UserError("Эта ссылка не поддерживается. Скачайте видео вручную и загрузите его как файл.", text)
    if "drm" in t:
        return UserError("Видео защищено DRM. Обработка таких видео невозможна.", text)
    if any(s in t for s in ("sign in", "login", "log in", "confirm your age", "age-restricted", "members-only",
                            "private video", "cookies")):
        return UserError(
            "Видео требует входа в аккаунт, подтверждения возраста или является приватным. "
            "ClipFactory не обходит ограничения доступа — используйте видео, на которое у вас есть права, "
            "и загрузите его файлом.", text)
    if any(s in t for s in ("video unavailable", "is unavailable", "not available", "has been removed",
                            "does not exist", "http error 404", "copyright")):
        return UserError("Видео недоступно: оно удалено, скрыто или ограничено в вашем регионе.", text)
    if "larger than max-filesize" in t or "max-filesize" in t:
        return UserError(f"Видео превышает лимит размера загрузки ({max_mb} МБ). Измените лимит в настройках.", text)
    if "http error 429" in t or "too many requests" in t:
        return UserError("Сайт временно ограничил скачивание (слишком много запросов). Попробуйте позже.", text)
    if "http error 403" in t:
        return UserError("Сайт запретил скачивание этого видео (ошибка 403).", text)
    if any(s in t for s in ("timed out", "timeout", "connection", "network", "getaddrinfo", "name or service",
                            "temporary failure", "urlopen error", "ssl", "reset by peer", "unreachable")):
        return UserError("Ошибка сети при скачивании. Проверьте подключение к интернету и попробуйте снова.", text)
    return UserError("Не удалось скачать видео по ссылке.", text)


class _QuietLogger:
    def debug(self, msg):  # noqa: D401
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.debug("yt-dlp: %s", msg)

    def error(self, msg):
        log.warning("yt-dlp: %s", msg)


def download_url(
    url: str,
    dest_dir: Path,
    file_stem: str,
    max_mb: int = 4096,
    on_progress: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[Path, str]:
    """Download a single video with yt-dlp. Returns (path, title)."""
    url = validate_url(url)
    try:
        import yt_dlp
    except ImportError as exc:
        raise UserError("Модуль yt-dlp не установлен. Запустите setup.bat ещё раз.") from exc
    dest_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = max_mb * 1024 * 1024

    def hook(d: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        if d.get("status") == "downloading" and on_progress:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total and total > max_bytes * 1.05:
                raise UserError(f"Видео превышает лимит размера загрузки ({max_mb} МБ).")
            frac = (done / total) if total else 0.0
            speed = d.get("speed") or 0
            msg = f"{done / 1048576:.1f} МБ"
            if total:
                msg += f" из {total / 1048576:.1f} МБ"
            if speed:
                msg += f" · {speed / 1048576:.1f} МБ/с"
            on_progress(max(0.0, min(1.0, frac)), msg)

    ff = config.find_binary("ffmpeg")
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        "outtmpl": str(dest_dir / f"{file_stem}.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlist_items": "1",
        "max_filesize": max_bytes,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "continuedl": True,
        "logger": _QuietLogger(),
        "allow_unplayable_formats": False,
    }
    if ff:
        opts["ffmpeg_location"] = os.path.dirname(ff)
    title = ""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and info.get("_type") == "playlist":
                entries = [e for e in info.get("entries") or [] if e]
                info = entries[0] if entries else info
            title = (info or {}).get("title") or ""
            if (info or {}).get("is_live"):
                raise UserError("Прямые трансляции не поддерживаются. Дождитесь окончания эфира.")
    except Cancelled:
        raise
    except UserError:
        raise
    except Exception as exc:  # yt_dlp.utils.DownloadError and friends
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled() from exc
        cause = exc
        while getattr(cause, "exc_info", None) and cause.exc_info and cause.exc_info[1] is not cause:
            inner = cause.exc_info[1]
            if isinstance(inner, (Cancelled, UserError)):
                raise inner from exc
            cause = inner
        raise _map_ytdlp_error(str(exc), max_mb) from exc

    candidates = [p for p in dest_dir.glob(f"{file_stem}.*")
                  if p.suffix.lower() in config.VIDEO_EXTENSIONS and not p.name.endswith(".part")]
    if not candidates:
        raise UserError(
            f"Видео не было скачано. Возможно, оно превышает лимит размера ({max_mb} МБ) "
            "или формат недоступен.")
    path = max(candidates, key=lambda p: p.stat().st_size)
    for extra in dest_dir.glob(f"{file_stem}.*"):
        if extra != path and (extra.suffix in (".part", ".ytdl") or ".f" in extra.name):
            try:
                extra.unlink()
            except OSError:
                pass
    return path, title


def check_upload_name(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in config.VIDEO_EXTENSIONS:
        allowed = ", ".join(sorted(config.VIDEO_EXTENSIONS))
        raise UserError(f"Неподдерживаемый тип файла «{ext or 'без расширения'}». Допустимы: {allowed}.")
    return ext


def remove_quietly(path: str | Path | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass

