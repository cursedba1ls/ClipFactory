"""Automatic publication of a series to TikTok (official Content Posting API), part after part.

The series gets a timetable: the first part at the chosen time (or as soon as it is ready), then one part
every N minutes, only inside the daily window and at most M parts a day. The scheduler (backend/jobs/
scheduler.py) publishes each part when its time comes, strictly in order: a part waits until the previous one
is published, until its video is ready and (unless switched off) until it is approved.
A part TikTok refused for a temporary reason is retried later; parts already published are never touched.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from backend.database import db
from backend.models.schemas import AutoPublish
from backend.services import settings_store

DONE = ("published", "manual", "inbox")
FIXED = DONE + ("publishing",)
MAX_ATTEMPTS = 3
RETRY_MINUTES = (15, 30, 60)


def settings_of(project: dict) -> AutoPublish:
    return settings_store.lenient(AutoPublish, (project.get("settings") or {}).get("autopublish") or {})


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _in_window(minute: float, window: tuple[int, int]) -> bool:
    a, b = window
    return a <= minute < b if a < b else (minute >= a or minute < b)  # a > b: window over midnight


def fit(t: float, window: tuple[int, int] | None, max_per_day: int, per_day: dict[str, int]) -> float:
    """Earliest moment >= t inside the daily window on a day that still has room (local time)."""
    for _ in range(800):
        d = datetime.fromtimestamp(t)
        midnight = d.replace(hour=0, minute=0, second=0, microsecond=0)
        if max_per_day and per_day.get(d.strftime("%Y-%m-%d"), 0) >= max_per_day:
            t = (midnight + timedelta(days=1)).timestamp()
            continue
        if window and not _in_window(d.hour * 60 + d.minute + d.second / 60, window):
            opens = midnight + timedelta(minutes=window[0])
            if opens.timestamp() < t:
                opens += timedelta(days=1)
            t = opens.timestamp()
            continue
        return t
    return t


def schedule_times(count: int, start: float, interval_minutes: int, window: tuple[int, int] | None = None,
                   max_per_day: int = 0, per_day: dict[str, int] | None = None) -> list[float]:
    """Times for `count` consecutive parts: start, then every interval, kept inside window and daily limit."""
    per_day = dict(per_day or {})
    out: list[float] = []
    t = start
    for _ in range(count):
        t = fit(t, window, max_per_day, per_day)
        out.append(round(t))
        day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
        per_day[day] = per_day.get(day, 0) + 1
        t += interval_minutes * 60
    return out


def _window(ap: AutoPublish) -> tuple[int, int] | None:
    a, b = _minutes(ap.window_start), _minutes(ap.window_end)
    return None if a == b else (a, b)


def _published_at(e: dict) -> float | None:
    info = e.get("publish_info") or {}
    return info.get("manual_at") or info.get("checked_at") or info.get("started_at") or e.get("scheduled_at")


def plan(project: dict, now: float | None = None, replan: bool = True) -> list[tuple[str, float]]:
    """(episode_id, time) for every part that still has to be published.

    replan=True rebuilds the timetable for all not yet published parts (after settings change);
    replan=False only gives a time to parts that have none yet (new parts of a running series).
    """
    ap = settings_of(project)
    now = now or time.time()
    eps = db.query("episodes", "project_id = ?", (project["id"],), "idx")
    fixed = [e for e in eps if e["publish_status"] in FIXED]
    per_day: dict[str, int] = {}
    last = None
    for e in fixed:
        t = _published_at(e)
        if t:
            day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
            per_day[day] = per_day.get(day, 0) + 1
            last = max(last or 0, t)
    todo = [e for e in eps if e["publish_status"] not in FIXED]
    if not replan:
        kept = [e for e in todo if e.get("scheduled_at") and (e.get("publish_info") or {}).get("autopublish")]
        for e in kept:
            day = datetime.fromtimestamp(e["scheduled_at"]).strftime("%Y-%m-%d")
            per_day[day] = per_day.get(day, 0) + 1
            last = max(last or 0, e["scheduled_at"])
        todo = [e for e in todo if e not in kept]
    if not todo:
        return []
    start = max(ap.start_at or now, now)
    if last:
        start = max(start, last + ap.interval_minutes * 60)
    times = schedule_times(len(todo), start, ap.interval_minutes, _window(ap), ap.max_per_day, per_day)
    return [(e["id"], t) for e, t in zip(todo, times)]


def _mark(e: dict, when: float, ap: AutoPublish) -> None:
    info = dict(e.get("publish_info") or {})
    for k in ("handled_schedule", "reminder", "error"):
        info.pop(k, None)
    info.update({"autopublish": True, "privacy_level": ap.privacy_level, "attempts": 0})
    db.update("episodes", e["id"], {
        "scheduled_at": when, "publish_status": "scheduled",
        "publish_mode": "api_direct" if ap.mode == "direct" else "api_inbox",
        "publish_info": info, "updated_at": db.now(),
    })


def apply(project_id: str, replan: bool = True) -> int:
    """Write the timetable. Returns how many parts got a (new) time."""
    p = db.get("projects", project_id)
    if not p:
        return 0
    ap = settings_of(p)
    if not ap.enabled:
        return 0
    items = plan(p, replan=replan)
    for eid, when in items:
        _mark(db.get("episodes", eid), when, ap)
    return len(items)


def stop(project_id: str) -> int:
    """Switch the series back to manual publication; parts already published or in progress stay as they are."""
    n = 0
    for e in db.query("episodes", "project_id = ? AND publish_status IN ('scheduled', 'failed', 'not_published')",
                      (project_id,)):
        info = dict(e.get("publish_info") or {})
        if not info.pop("autopublish", None):
            continue
        for k in ("handled_schedule", "reminder", "attempts"):
            info.pop(k, None)
        db.update("episodes", e["id"], {"scheduled_at": None, "publish_status": "not_published",
                                        "publish_mode": "manual", "publish_info": info, "updated_at": db.now()})
        n += 1
    return n


def retry_failed(project: dict, now: float | None = None) -> None:
    """A part TikTok refused gets up to MAX_ATTEMPTS more tries (15 / 30 / 60 minutes later; a daily-limit
    refusal waits for the next day). After that the series pauses at this part until the user acts."""
    now = now or time.time()
    ap = settings_of(project)
    for e in db.query("episodes", "project_id = ? AND publish_status = 'failed'", (project["id"],)):
        info = dict(e.get("publish_info") or {})
        if not info.get("autopublish"):
            continue
        attempts = int(info.get("attempts") or 0)
        if attempts >= MAX_ATTEMPTS:
            continue
        error = str(info.get("error") or "")
        if "на сегодня" in error:  # TikTok's daily posting limit
            tomorrow = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0) + timedelta(days=1)
            when = fit(tomorrow.timestamp(), _window(ap), 0, {})
        else:
            when = now + RETRY_MINUTES[attempts] * 60
        info.update({"attempts": attempts + 1, "reminder": f"Повторная попытка {attempts + 1} из {MAX_ATTEMPTS}: "
                                                             f"{error}" if error else None})
        info.pop("handled_schedule", None)
        db.update("episodes", e["id"], {"scheduled_at": when, "publish_status": "scheduled",
                                        "publish_info": info, "updated_at": db.now()})


def waiting_reason(project: dict, e: dict, eps: list[dict], api_ready: bool) -> str | None:
    """Why a due part of an autopublished series cannot go out yet (None = publish now)."""
    ap = settings_of(project)
    earlier = [x for x in eps if x["idx"] < e["idx"] and x["publish_status"] not in DONE]
    if earlier:
        first = earlier[0]
        if first["publish_status"] == "failed":
            return f"Серия на паузе: часть {first['idx'] + 1} не опубликовалась. Опубликуйте её или пропустите."
        return f"Ждёт публикации части {first['idx'] + 1}."
    if e["status"] != "ready":
        return "Ждёт, пока видео части будет создано."
    if not e["approved"] and ap.require_approval:
        return "Ждёт одобрения части."
    if not api_ready:
        return "Аккаунт TikTok не подключён — подключите его в настройках."
    return None


def refresh_publishing(project: dict, client, now: float | None = None, every: float = 300) -> None:
    """Ask TikTok again about parts still "publishing" (TikTok may take longer than the publish job waits),
    so the next part of the series is not held back forever."""
    from backend.services import publisher

    now = now or time.time()
    for e in db.query("episodes", "project_id = ? AND publish_status = 'publishing'", (project["id"],)):
        info = dict(e.get("publish_info") or {})
        pid = info.get("publish_id")
        if not info.get("autopublish") or not pid or now - float(info.get("checked_at") or 0) < every:
            continue
        try:
            data = client.fetch_status(pid)
        except Exception as exc:  # network or token problem: try again on a later tick
            info.update({"checked_at": now, "message": str(getattr(exc, "message", exc))})
            db.update("episodes", e["id"], {"publish_info": info})
            continue
        status, msg, _final = publisher.interpret_status(data, info.get("mode", "direct"))
        info.update({"status_raw": data.get("status"), "message": msg, "checked_at": now})
        if status == "failed":
            info["error"] = msg
        db.update("episodes", e["id"], {"publish_status": status, "publish_info": info, "updated_at": db.now()})
