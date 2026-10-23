"""Local publication scheduler.

Every 30 s it checks episodes whose scheduled time has come:
  * a series with automatic publication (backend/services/autopublish.py): new parts get their time, refused
    parts are retried, and the due part goes out through the TikTok API once the previous part is published,
    its video is ready and it is approved (approval can be switched off for the series);
  * other parts: approved + API mode + TikTok connected → queue an official API publish job;
    otherwise → a reminder so the UI prompts the user to upload manually.
Unapproved episodes are never published unless the series explicitly allows it.
"""
from __future__ import annotations

import logging
import threading

from backend.database import db
from backend.jobs.queue import manager

log = logging.getLogger(__name__)


def tick() -> None:
    from backend.jobs.pipeline import tiktok_client
    from backend.services import autopublish

    client = tiktok_client()
    api_ready = client.is_configured() and bool(client.tokens())
    auto = {}
    for p in db.query("projects"):
        if autopublish.settings_of(p).enabled:
            auto[p["id"]] = p
            autopublish.retry_failed(p)
            autopublish.apply(p["id"], replan=False)  # parts created since the timetable was made
            if api_ready:
                autopublish.refresh_publishing(p, client)

    due = db.query("episodes", "scheduled_at IS NOT NULL AND scheduled_at <= ? "
                               "AND publish_status IN ('not_published', 'scheduled', 'failed')", (db.now(),),
                   "project_id, idx")
    if not due:
        return
    series: dict[str, list[dict]] = {}
    for ep in due:
        info = dict(ep.get("publish_info") or {})
        project = auto.get(ep["project_id"])
        if project and info.get("autopublish"):
            if ep["publish_status"] == "failed":
                continue  # retries are scheduled by retry_failed; out of attempts = the series is paused
            eps = series.get(project["id"])
            if eps is None:
                eps = series[project["id"]] = db.query("episodes", "project_id = ?", (project["id"],), "idx")
            reason = autopublish.waiting_reason(project, ep, eps, api_ready)
            if reason:
                if info.get("reminder") != reason:
                    info["reminder"] = reason
                    db.update("episodes", ep["id"], {"publish_info": info})
                continue
            ap = autopublish.settings_of(project)
            if not ep["approved"]:
                db.update("episodes", ep["id"], {"approved": 1})  # the series publishes without approval
            payload = {"mode": ap.mode, "privacy_level": ap.privacy_level, "disable_comment": ap.disable_comment,
                       "disable_duet": ap.disable_duet, "disable_stitch": ap.disable_stitch}
            manager.enqueue("tiktok_publish", project_id=ep["project_id"], episode_id=ep["id"], payload=payload)
            info.update({"reminder": None, "handled_schedule": ep["scheduled_at"]})
            db.update("episodes", ep["id"], {"publish_info": info, "publish_status": "publishing"})
            for x in eps:  # the next part must wait for this one
                if x["id"] == ep["id"]:
                    x["publish_status"] = "publishing"
            continue
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
