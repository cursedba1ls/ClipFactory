"""Local publication scheduler.

Every 30 s it checks episodes whose scheduled time has come:
  * approved + API mode + TikTok connected → queue an official API publish job;
  * otherwise → mark a reminder so the UI prompts the user to upload manually.
Unapproved episodes are never published.
"""
from __future__ import annotations

import logging
import threading

from backend.database import db
from backend.jobs.queue import manager

log = logging.getLogger(__name__)


def tick() -> None:
    from backend.jobs.pipeline import tiktok_client

    due = db.query("episodes", "scheduled_at IS NOT NULL AND scheduled_at <= ? "
                               "AND publish_status IN ('not_published', 'scheduled', 'failed')", (db.now(),))
    if not due:
        return
    client = tiktok_client()
    api_ready = client.is_configured() and bool(client.tokens())
    for ep in due:
        info = dict(ep.get("publish_info") or {})
        if info.get("handled_schedule") == ep["scheduled_at"]:
            continue
        info["handled_schedule"] = ep["scheduled_at"]
        if not ep["approved"]:
            info["reminder"] = "Время публикации наступило, но часть не одобрена — публикация не выполнена."
            db.update("episodes", ep["id"], {"publish_info": info})
            continue
        if ep["publish_mode"] in ("api_direct", "api_inbox") and api_ready and ep["status"] == "ready":
            mode = "direct" if ep["publish_mode"] == "api_direct" else "inbox"
            payload = {"mode": mode, "privacy_level": info.get("privacy_level", "SELF_ONLY")}
            manager.enqueue("tiktok_publish", project_id=ep["project_id"], episode_id=ep["id"], payload=payload)
            info["reminder"] = None
            db.update("episodes", ep["id"], {"publish_info": info, "publish_status": "publishing"})
        else:
            info["reminder"] = "Пора опубликовать эту часть вручную."
            db.update("episodes", ep["id"], {"publish_info": info})


class Scheduler:
    def __init__(self, interval: float = 30.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                tick()
            except Exception:
                log.exception("scheduler tick failed")


scheduler = Scheduler()
