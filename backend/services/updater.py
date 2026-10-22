"""Automatic updates for the installed app.

1. check    — download latest.json from GitHub Releases, verify its Ed25519 signature (keys in
              backend/update_keys.py) and compare versions;
2. download — fetch the installer over HTTPS into storage/updates, verifying size and SHA-256 from the
              signed manifest (a file that does not match is deleted, never run);
3. install  — back up the database, close the app and run the installer silently (/VERYSILENT /UPDATE).
              The installer replaces only program files (user data lives in a separate folder), then
              starts the new version, which confirms the update and cleans up.

Every step can fail without harming the installed version: nothing is replaced until the installer runs,
and the installer itself rolls back if it cannot finish.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from backend import build_info, config
from backend.errors import UserError
from backend.services import update_manifest
from backend.services.update_manifest import ManifestError

log = logging.getLogger(__name__)

DEFAULT_MANIFEST_URL = "https://github.com/cursedba1ls/ClipFactory/releases/latest/download/latest.json"
ALLOWED_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
CHECK_INTERVAL = 6 * 3600
FIRST_CHECK_DELAY = 20
MAX_MANIFEST_BYTES = 256 * 1024
USER_AGENT = f"ClipFactory-Updater/{config.APP_VERSION}"


def public_keys() -> list[str]:
    from backend.update_keys import PUBLIC_KEYS

    return list(PUBLIC_KEYS) + [k for k in build_info.info().get("extra_public_keys") or [] if k]


def manifest_url() -> str:
    url = os.environ.get("CLIPFACTORY_UPDATE_URL") or build_info.info().get("update_url") or DEFAULT_MANIFEST_URL
    return url


def _insecure_allowed() -> bool:
    return bool(build_info.info().get("allow_insecure_updates"))


def check_url(url: str) -> None:
    """Only HTTPS to GitHub; test builds may also use http://127.0.0.1 (local update server in CI)."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if _insecure_allowed() and p.scheme == "http" and host in ("127.0.0.1", "localhost"):
        return
    if p.scheme != "https" or host not in ALLOWED_HOSTS:
        raise UserError(f"Адрес обновления не разрешён: {host or url}")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float = 30):
    check_url(url)
    opener = urllib.request.build_opener(_CheckedRedirects())
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
    return opener.open(req, timeout=timeout)


def _net_error(exc: Exception) -> UserError:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return UserError("Сведения об обновлениях пока не опубликованы.", str(exc))
        return UserError(f"Сервер обновлений ответил ошибкой {exc.code}.", str(exc))
    return UserError("Нет связи с сервером обновлений. Проверьте подключение к интернету.", str(exc))


class Updater:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.state = "idle"  # idle | checking | available | downloading | ready | installing | error
        self.available: dict | None = None
        self.progress = 0.0
        self.error: str | None = None
        self.last_check: float | None = None
        self.just_updated: dict | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.installer_launcher: Callable[[Path], None] | None = None  # set by the desktop shell

    # ------------------------------------------------------------------ paths / persisted state
    @property
    def dir(self) -> Path:
        d = config.DIRS["updates"]
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _state_file(self) -> Path:
        return self.dir / "state.json"

    def _load(self) -> dict:
        try:
            return json.loads(self._state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        tmp = self._state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._state_file())

    # ------------------------------------------------------------------ public API
    def status(self) -> dict:
        with self.lock:
            return {
                "current_version": config.APP_VERSION,
                "installed_build": build_info.is_installed_build(),
                "channel": build_info.info().get("channel"),
                "state": self.state,
                "available": self.available,
                "progress": round(self.progress, 3),
                "error": self.error,
                "last_check": self.last_check,
                "just_updated": self.just_updated,
                "manifest_url": manifest_url(),
            }

    def startup(self) -> None:
        """Called once at app start: confirm a finished update, restore a downloaded installer."""
        data = self._load()
        pending = data.pop("pending_install", None)
        if pending:
            if pending.get("version") == config.APP_VERSION:
                self.just_updated = {"from": pending.get("from_version"), "to": config.APP_VERSION,
                                     "notes": pending.get("notes") or ""}
                hist = data.setdefault("history", [])
                hist.append({"from": pending.get("from_version"), "to": config.APP_VERSION, "at": time.time()})
                data["history"] = hist[-20:]
                log.info("updated from %s to %s", pending.get("from_version"), config.APP_VERSION)
            else:
                self.error = (f"Обновление до версии {pending.get('version')} не установилось. "
                              f"Работает версия {config.APP_VERSION}; попробуйте ещё раз.")
                log.warning("update to %s did not complete (running %s)", pending.get("version"), config.APP_VERSION)
        dl = data.get("downloaded")
        if dl:
            path = Path(dl.get("path", ""))
            try:
                newer = update_manifest.is_newer(dl.get("version", "0.0.0"), config.APP_VERSION)
            except ManifestError:
                newer = False
            if newer and path.is_file() and path.parent == self.dir:
                self.available = dl["payload"]
                self.state = "ready"
            else:
                data.pop("downloaded", None)
        self._save(data)
        self._cleanup(keep=Path(dl["path"]) if dl and self.state == "ready" else None)

    def dismiss_notice(self) -> None:
        with self.lock:
            self.just_updated = None

    def check(self) -> dict:
        with self.lock:
            if self.state in ("checking", "downloading", "installing"):
                return self.status()
            self.state, self.error = "checking", None
        try:
            payload = self._fetch_manifest()
        except (UserError, ManifestError) as exc:
            with self.lock:
                self.error = exc.message if isinstance(exc, UserError) else str(exc)
                self.last_check = time.time()
                # A failed re-check never throws away an update that is already downloaded and verified.
                self.state = "ready" if self._restore_downloaded() else "error"
            log.info("update check failed: %s", getattr(exc, "detail", None) or exc)
            return self.status()
        with self.lock:
            self.last_check = time.time()
            if payload and update_manifest.is_newer(payload["version"], config.APP_VERSION):
                ready = self._downloaded_matches(payload)
                self.available = payload
                self.state = "ready" if ready else "available"
            elif self._restore_downloaded():
                self.state = "ready"
            else:
                self.available = None
                self.state = "idle"
        return self.status()

    def _restore_downloaded(self) -> bool:
        """Make a previously downloaded, still newer installer the available update again."""
        dl = self._load().get("downloaded") or {}
        try:
            newer = update_manifest.is_newer(dl.get("version", "0.0.0"), config.APP_VERSION)
        except ManifestError:
            return False
        if newer and Path(dl.get("path", "")).is_file() and dl.get("payload"):
            self.available = dl["payload"]
            return True
        return False

    def _fetch_manifest(self) -> dict | None:
        url = manifest_url()
        try:
            with _open(url) as resp:
                raw = resp.read(MAX_MANIFEST_BYTES + 1)
        except UserError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                return None  # no signed release published yet
            raise _net_error(exc) from exc
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ManifestError("Файл обновления слишком большой.")
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ManifestError("Файл обновления повреждён.") from exc
        payload = update_manifest.verify(manifest, public_keys())
        check_url(payload["installer"]["url"])
        return payload

    def _downloaded_matches(self, payload: dict) -> bool:
        dl = self._load().get("downloaded") or {}
        p = Path(dl.get("path", ""))
        return dl.get("sha256") == payload["installer"]["sha256"] and p.is_file()

    def download(self, wait: bool = False) -> dict:
        with self.lock:
            if self.state == "ready" or self.state == "downloading":
                return self.status()
            if not self.available:
                raise UserError("Нет доступного обновления. Сначала проверьте обновления.")
            payload = self.available
            self.state, self.progress, self.error = "downloading", 0.0, None
            self._cancel.clear()
        t = threading.Thread(target=self._download, args=(payload,), name="update-download", daemon=True)
        t.start()
        if wait:
            t.join()
        return self.status()

    def cancel_download(self) -> None:
        self._cancel.set()

    def _download(self, payload: dict) -> None:
        inst = payload["installer"]
        dest = self.dir / inst["name"]
        part = dest.with_suffix(".part")
        try:
            h = hashlib.sha256()
            done = 0
            with _open(inst["url"], timeout=60) as resp, part.open("wb") as f:
                while True:
                    if self._cancel.is_set():
                        raise UserError("Загрузка обновления отменена.")
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > inst["size"]:
                        raise UserError("Размер установщика не совпадает с ожидаемым — загрузка прервана.")
                    h.update(chunk)
                    f.write(chunk)
                    with self.lock:
                        self.progress = done / inst["size"]
            if done != inst["size"] or h.hexdigest() != inst["sha256"]:
                raise UserError("Контрольная сумма установщика не совпала — файл удалён. Попробуйте ещё раз.")
            part.replace(dest)
            data = self._load()
            data["downloaded"] = {"version": payload["version"], "path": str(dest), "sha256": inst["sha256"],
                                  "payload": payload}
            self._save(data)
            self._cleanup(keep=dest)
            with self.lock:
                self.state, self.progress = "ready", 1.0
            log.info("update %s downloaded", payload["version"])
        except Exception as exc:  # network, disk, verification
            part.unlink(missing_ok=True)
            err = exc if isinstance(exc, UserError) else _net_error(exc)
            with self.lock:
                self.state, self.error = "error", err.message
            log.warning("update download failed: %s", getattr(err, "detail", None) or err.message)

    def verify_file(self, path: Path, sha256: str) -> bool:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest() == sha256

    def install(self) -> dict:
        """Prepare and hand over to the installer (the desktop shell closes the app and runs it)."""
        from backend.services import backup

        with self.lock:
            if self.state != "ready" or not self.available:
                raise UserError("Обновление ещё не загружено.")
            if not build_info.is_installed_build() or self.installer_launcher is None:
                raise UserError("Обновления устанавливаются только в установленной программе ClipFactory.")
            dl = self._load().get("downloaded") or {}
            path = Path(dl.get("path", ""))
            if not path.is_file() or not self.verify_file(path, dl.get("sha256", "")):
                self.state, self.error = "error", "Файл обновления повреждён — загрузите его заново."
                data = self._load()
                data.pop("downloaded", None)
                self._save(data)
                path.unlink(missing_ok=True)
                raise UserError(self.error)
            self.state = "installing"
        backup.backup_database(f"update-{config.APP_VERSION}-to-{self.available['version']}")
        data = self._load()
        data["pending_install"] = {"version": self.available["version"], "from_version": config.APP_VERSION,
                                   "notes": self.available.get("notes") or "", "started_at": time.time()}
        self._save(data)
        log.info("installing update %s", self.available["version"])
        self.installer_launcher(path)
        return self.status()

    def _cleanup(self, keep: Path | None) -> None:
        for p in self.dir.glob("*"):
            if p.suffix in (".exe", ".part") and (keep is None or p != keep):
                try:
                    p.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------------ background schedule
    def start_background(self, settings_getter) -> None:
        if self._thread or not build_info.is_installed_build():
            return

        def loop() -> None:
            if self._stop.wait(FIRST_CHECK_DELAY):
                return
            while not self._stop.is_set():
                try:
                    s = settings_getter()
                    if s.update_check:
                        st = self.check()
                        if st["state"] == "available" and s.update_auto_download:
                            self.download()
                except Exception:
                    log.exception("background update check failed")
                if self._stop.wait(CHECK_INTERVAL):
                    return

        self._thread = threading.Thread(target=loop, name="update-check", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()


def launch_installer(path: Path) -> None:
    """Start the installer detached from this process (it waits until the app has exited)."""
    log_path = path.with_suffix(".log")
    args = [str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/UPDATE", f"/LOG={log_path}"]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x01000000  # BREAKAWAY_FROM_JOB
    try:
        subprocess.Popen(args, close_fds=True, creationflags=flags, cwd=str(path.parent))
    except OSError:
        # Some job objects forbid breakaway: start without it.
        flags &= ~0x01000000
        subprocess.Popen(args, close_fds=True, creationflags=flags, cwd=str(path.parent))


updater = Updater()
