"""TikTok publication: manual workflow helpers and the official Content Posting API.

Only official, documented endpoints are used (OAuth 2.0 + Content Posting API).
No cookies, no browser automation, no anti-bot evasion. Success is reported only
after TikTok's status endpoint confirms it.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import math
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from backend.errors import Cancelled, UserError
from backend.services import secure_store

log = logging.getLogger(__name__)

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name,avatar_url"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
DIRECT_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
SCOPES = "user.info.basic,video.upload,video.publish"
MANUAL_UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"

MB = 1024 * 1024
MAX_SINGLE_CHUNK = 64 * MB
CHUNK_SIZE = 10 * MB

# Final statuses reported by /v2/post/publish/status/fetch/
STATUS_TEXT = {
    "PROCESSING_UPLOAD": "TikTok принимает файл",
    "PROCESSING_DOWNLOAD": "TikTok загружает файл",
    "SEND_TO_USER_INBOX": "Видео отправлено во «Входящие» TikTok — завершите публикацию в приложении",
    "PUBLISH_COMPLETE": "Публикация подтверждена TikTok",
    "FAILED": "TikTok отклонил публикацию",
}

ERROR_TEXT = {
    "access_token_invalid": "Токен доступа TikTok недействителен или истёк. Подключите аккаунт заново.",
    "scope_not_authorized": "Аккаунт не выдал приложению нужные права (video.upload / video.publish). "
                            "Подключите аккаунт заново и разрешите публикацию.",
    "scope_permission_missed": "У приложения TikTok нет одобренного доступа к Content Posting API.",
    "rate_limit_exceeded": "Превышен лимит запросов TikTok API. Повторите попытку через минуту.",
    "spam_risk_too_many_posts": "TikTok ограничил количество публикаций для этого аккаунта на сегодня.",
    "spam_risk_user_banned_from_posting": "Этому аккаунту временно запрещено публиковать видео.",
    "spam_risk_too_many_pending_share": "Слишком много неопубликованных видео во «Входящих» TikTok. "
                                        "Завершите или удалите их в приложении.",
    "unaudited_client_can_only_post_to_private_accounts":
        "Приложение TikTok ещё не прошло аудит: прямая публикация доступна только с приватностью "
        "«Только я» (SELF_ONLY) или для приватного аккаунта. Используйте ручную публикацию или режим «Входящие».",
    "privacy_level_option_mismatch": "Выбранный уровень приватности недоступен для этого аккаунта.",
    "invalid_file_upload": "TikTok не принял файл: проверьте формат и длительность видео.",
    "reached_active_user_cap": "Достигнут дневной лимит активных пользователей приложения TikTok.",
    "invalid_params": "TikTok отклонил параметры запроса.",
}

PRIVACY_NAMES = {
    "PUBLIC_TO_EVERYONE": "Все",
    "MUTUAL_FOLLOW_FRIENDS": "Взаимные подписчики",
    "FOLLOWER_OF_CREATOR": "Подписчики",
    "SELF_ONLY": "Только я",
}


class RateLimiter:
    """Sliding-window limiter (e.g. 6 init requests per minute per token)."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self, cancel_event: threading.Event | None = None, sleep=time.sleep) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                delay = self.period - (now - self._calls[0]) + 0.05
            if cancel_event is not None and cancel_event.is_set():
                raise Cancelled()
            sleep(min(delay, 1.0))


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) following TikTok FILE_UPLOAD rules."""
    if size <= 0:
        raise UserError("Файл видео пустой.")
    if size <= MAX_SINGLE_CHUNK:
        return size, 1
    count = size // CHUNK_SIZE
    return CHUNK_SIZE, count


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")[:64]
    # TikTok expects the hex-encoded SHA256 of the verifier for desktop/loopback apps.
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    return verifier, challenge


class TikTokError(UserError):
    def __init__(self, message: str, code: str = "", detail: str | None = None):
        super().__init__(message, detail)
        self.code = code


class TikTokClient:
    """Official TikTok Login Kit + Content Posting API client."""

    def __init__(self, client_key: str, redirect_uri: str, http=None):
        self.client_key = client_key
        self.redirect_uri = redirect_uri
        self._http = http
        self._states: dict[str, tuple[str, float]] = {}
        self.init_limiter = RateLimiter(6, 60)
        self.status_limiter = RateLimiter(30, 60)

    # --- plumbing -------------------------------------------------------
    @property
    def http(self):
        if self._http is None:
            import httpx
            self._http = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
        return self._http

    @property
    def client_secret(self) -> str:
        return secure_store.get_secret("tiktok_client_secret") or ""

    def is_configured(self) -> bool:
        return bool(self.client_key and self.client_secret and self.redirect_uri)

    @staticmethod
    def _parse(resp) -> dict:
        try:
            data = resp.json()
        except Exception:
            raise TikTokError(f"TikTok вернул некорректный ответ (HTTP {resp.status_code}).")
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict) and err.get("code") not in (None, "", "ok"):
            code = err.get("code", "")
            msg = ERROR_TEXT.get(code) or f"Ошибка TikTok API: {err.get('message') or code}"
            raise TikTokError(msg, code=code, detail=f"log_id={err.get('log_id', '')}")
        if isinstance(data, dict) and "error" in data and isinstance(data["error"], str):
            # OAuth endpoints use {"error": "...", "error_description": "..."}
            raise TikTokError(f"Ошибка авторизации TikTok: {data.get('error_description') or data['error']}",
                              code=data["error"])
        if resp.status_code >= 400:
            raise TikTokError(f"Ошибка TikTok API (HTTP {resp.status_code}).")
        return data

    # --- OAuth ----------------------------------------------------------
    def build_auth_url(self) -> str:
        if not self.is_configured():
            raise UserError("Сначала укажите Client Key, Client Secret и Redirect URI приложения TikTok в настройках.")
        state = secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        self._states = {k: v for k, v in self._states.items() if v[1] > time.time()}
        self._states[state] = (verifier, time.time() + 900)
        params = {
            "client_key": self.client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return AUTH_URL + "?" + urlencode(params)

    def exchange_code(self, code: str, state: str) -> dict:
        entry = self._states.pop(state, None)
        if not entry or entry[1] < time.time():
            raise UserError("Сессия авторизации TikTok устарела или подделана. Начните подключение заново.")
        resp = self.http.post(TOKEN_URL, data={
            "client_key": self.client_key,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
            "code_verifier": entry[0],
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        data = self._parse(resp)
        self._store_tokens(data)
        return data

    def _store_tokens(self, data: dict) -> None:
        if not data.get("access_token"):
            raise TikTokError("TikTok не вернул токен доступа.")
        now = time.time()
        secure_store.set_secret("tiktok_tokens", {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "expires_at": now + float(data.get("expires_in") or 0),
            "refresh_expires_at": now + float(data.get("refresh_expires_in") or 0),
            "open_id": data.get("open_id"),
            "scope": data.get("scope", ""),
        })

    def tokens(self) -> dict | None:
        return secure_store.get_secret("tiktok_tokens")

    def access_token(self) -> str:
        tok = self.tokens()
        if not tok:
            raise UserError("Аккаунт TikTok не подключён.")
        if tok.get("expires_at", 0) - time.time() < 300:
            tok = self.refresh()
        return tok["access_token"]

    def refresh(self) -> dict:
        tok = self.tokens()
        if not tok or not tok.get("refresh_token"):
            raise UserError("Сессия TikTok истекла. Подключите аккаунт заново.")
        if tok.get("refresh_expires_at") and tok["refresh_expires_at"] < time.time():
            raise UserError("Сессия TikTok истекла. Подключите аккаунт заново.")
        resp = self.http.post(TOKEN_URL, data={
            "client_key": self.client_key,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        self._store_tokens(self._parse(resp))
        return self.tokens()

    def disconnect(self) -> None:
        tok = self.tokens()
        if tok and self.client_secret:
            try:
                self.http.post(REVOKE_URL, data={"client_key": self.client_key, "client_secret": self.client_secret,
                                                 "token": tok.get("access_token", "")},
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
            except Exception:
                log.warning("TikTok token revoke failed")
        secure_store.set_secret("tiktok_tokens", None)

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token()}", "Content-Type": "application/json; charset=UTF-8"}

    # --- API ------------------------------------------------------------
    def user_info(self) -> dict:
        resp = self.http.get(USER_INFO_URL, headers=self._auth_headers())
        return (self._parse(resp).get("data") or {}).get("user") or {}

    def creator_info(self) -> dict:
        resp = self.http.post(CREATOR_INFO_URL, headers=self._auth_headers(), json={})
        return self._parse(resp).get("data") or {}

    def publish_video(self, path: Path, caption: str, mode: str = "direct", privacy_level: str = "SELF_ONLY",
                      disable_comment: bool = False, disable_duet: bool = False, disable_stitch: bool = False,
                      duration: float | None = None,
                      on_progress: Callable[[float, str], None] | None = None,
                      cancel_event: threading.Event | None = None) -> str:
        """Initialise the post and upload the file. Returns publish_id (status must be polled)."""
        size = path.stat().st_size
        chunk_size, count = chunk_plan(size)
        source_info = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size,
                       "total_chunk_count": count}
        if mode == "direct":
            info = self.creator_info()
            options = info.get("privacy_level_options") or []
            if options and privacy_level not in options:
                names = ", ".join(PRIVACY_NAMES.get(o, o) for o in options)
                raise UserError(f"Уровень приватности недоступен. Доступно для аккаунта: {names}.")
            max_dur = info.get("max_video_post_duration_sec")
            body = {
                "post_info": {
                    "title": caption[:2200],
                    "privacy_level": privacy_level,
                    "disable_comment": bool(disable_comment or info.get("comment_disabled")),
                    "disable_duet": bool(disable_duet or info.get("duet_disabled")),
                    "disable_stitch": bool(disable_stitch or info.get("stitch_disabled")),
                    "video_cover_timestamp_ms": 1000,
                },
                "source_info": source_info,
            }
            url = DIRECT_INIT_URL
            if max_dur and duration and duration > float(max_dur):
                raise UserError(f"Видео длиннее, чем разрешено для этого аккаунта TikTok ({max_dur} с).")
        else:
            body = {"source_info": source_info}
            url = INBOX_INIT_URL
        self.init_limiter.wait(cancel_event)
        resp = self.http.post(url, headers=self._auth_headers(), json=body)
        data = self._parse(resp).get("data") or {}
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise TikTokError("TikTok не вернул адрес для загрузки видео.")
        self._upload(path, upload_url, size, chunk_size, count, on_progress, cancel_event)
        return publish_id

    def _upload(self, path: Path, upload_url: str, size: int, chunk_size: int, count: int,
                on_progress, cancel_event) -> None:
        with path.open("rb") as f:
            for i in range(count):
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled()
                first = i * chunk_size
                last = size - 1 if i == count - 1 else first + chunk_size - 1
                f.seek(first)
                chunk = f.read(last - first + 1)
                headers = {"Content-Type": "video/mp4", "Content-Length": str(len(chunk)),
                           "Content-Range": f"bytes {first}-{last}/{size}"}
                for attempt in range(3):
                    resp = self.http.put(upload_url, content=chunk, headers=headers)
                    if resp.status_code in (200, 201, 206):
                        break
                    if attempt == 2:
                        raise TikTokError(f"Ошибка загрузки файла в TikTok (HTTP {resp.status_code}).")
                    time.sleep(2 * (attempt + 1))
                if on_progress:
                    on_progress((i + 1) / count, f"Загружено {math.ceil((last + 1) / MB)} из {math.ceil(size / MB)} МБ")

    def fetch_status(self, publish_id: str, cancel_event: threading.Event | None = None) -> dict:
        self.status_limiter.wait(cancel_event)
        resp = self.http.post(STATUS_URL, headers=self._auth_headers(), json={"publish_id": publish_id})
        return self._parse(resp).get("data") or {}


def interpret_status(data: dict, mode: str) -> tuple[str, str, bool]:
    """Map a status payload to (publish_status, message, final)."""
    st = data.get("status", "")
    if st == "PUBLISH_COMPLETE":
        return "published", STATUS_TEXT[st], True
    if st == "SEND_TO_USER_INBOX":
        return "inbox", STATUS_TEXT[st], True
    if st == "FAILED":
        reason = data.get("fail_reason") or ""
        return "failed", f"{STATUS_TEXT[st]}: {reason}" if reason else STATUS_TEXT[st], True
    return "publishing", STATUS_TEXT.get(st, "Ожидание ответа TikTok"), False


_client: TikTokClient | None = None
_client_lock = threading.Lock()


def get_client(client_key: str, redirect_uri: str) -> TikTokClient:
    """Shared client so OAuth state survives between /auth-url and /callback."""
    global _client
    with _client_lock:
        if _client is None:
            _client = TikTokClient(client_key, redirect_uri)
        else:
            _client.client_key = client_key
            _client.redirect_uri = redirect_uri
        return _client
