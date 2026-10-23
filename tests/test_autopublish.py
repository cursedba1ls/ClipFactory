"""Automatic TikTok publication of a series: timetable, order, approval, retries, API."""
from datetime import datetime

import pytest

from backend.database import db
from backend.jobs import pipeline, scheduler
from backend.services import autopublish


def ts(*a):
    return datetime(*a).timestamp()


def fmt(times):
    return [datetime.fromtimestamp(t).strftime("%d %H:%M") for t in times]


def test_timetable_respects_window_and_daily_limit():
    times = autopublish.schedule_times(6, ts(2026, 10, 24, 21, 30), 120, (600, 1380), 3)
    assert fmt(times) == ["24 21:30", "25 10:00", "25 12:00", "25 14:00", "26 10:00", "26 12:00"]
    # window over midnight (22:00–02:00)
    assert fmt(autopublish.schedule_times(3, ts(2026, 10, 24, 3, 0), 60, (1320, 120))) == \
        ["24 22:00", "24 23:00", "25 00:00"]
    assert fmt(autopublish.schedule_times(2, ts(2026, 10, 24, 9, 0), 30)) == ["24 09:00", "24 09:30"]


class FakeClient:
    def __init__(self, connected=True):
        self.connected = connected
        self.status = "PUBLISH_COMPLETE"

    def is_configured(self):
        return self.connected

    def tokens(self):
        return {"access_token": "x"} if self.connected else None

    def fetch_status(self, pid):
        return {"status": self.status}

    def creator_info(self):
        return {"privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"]}


@pytest.fixture
def series(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(pipeline, "tiktok_client", lambda: fake)
    queued = []
    pid = db.new_id()
    # only this test's series: projects left by other tests may publish their own due parts
    monkeypatch.setattr(scheduler.manager, "enqueue",
                        lambda *a, **k: (k["project_id"] == pid and queued.append(k)) or {"id": "j"})
    t = db.now()

    def make(settings: dict, n=3, approved=1, status="ready"):
        db.insert("projects", {"id": pid, "title": "Серия", "created_at": t, "updated_at": t,
                               "settings": {"autopublish": {"enabled": True, **settings}}})
        for i in range(n):
            db.insert("episodes", {"id": db.new_id(), "project_id": pid, "idx": i, "start_frame": i * 10,
                                   "end_frame": i * 10 + 10, "start_time": i, "end_time": i + 1, "duration": 1,
                                   "status": status, "approved": approved, "updated_at": t})
        return pid

    def eps():
        return db.query("episodes", "project_id = ?", (pid,), "idx")

    return make, eps, queued, fake


def test_apply_plans_in_order_then_publishes_one_by_one(series):
    make, eps, queued, fake = series
    now = db.now()
    pid = make({"start_at": now - 3600, "interval_minutes": 5, "window_start": "00:00", "window_end": "00:00",
                "max_per_day": 0, "mode": "direct", "privacy_level": "PUBLIC_TO_EVERYONE", "disable_duet": True})
    assert autopublish.apply(pid) == 3
    parts = eps()
    times = [e["scheduled_at"] for e in parts]
    assert times == sorted(times) and times[1] - times[0] == 300
    assert all(e["publish_status"] == "scheduled" and e["publish_mode"] == "api_direct" for e in parts)
    # make all parts due
    for e in parts:
        db.update("episodes", e["id"], {"scheduled_at": now - 10})
    scheduler.tick()
    assert [q["episode_id"] for q in queued] == [parts[0]["id"]], "only the first part goes out"
    assert queued[0]["payload"]["privacy_level"] == "PUBLIC_TO_EVERYONE" and queued[0]["payload"]["disable_duet"]
    second = db.get("episodes", parts[1]["id"])
    assert "Ждёт публикации части 1" in second["publish_info"]["reminder"]
    db.update("episodes", parts[0]["id"], {"publish_status": "published"})
    scheduler.tick()
    assert [q["episode_id"] for q in queued][-1] == parts[1]["id"]


def test_new_parts_get_times_automatically(series):
    make, eps, queued, fake = series
    pid = make({"interval_minutes": 60, "window_start": "00:00", "window_end": "00:00"}, n=2)
    autopublish.apply(pid)
    t = db.now()
    db.insert("episodes", {"id": db.new_id(), "project_id": pid, "idx": 2, "start_frame": 20, "end_frame": 30,
                           "start_time": 2, "end_time": 3, "duration": 1, "status": "pending", "updated_at": t})
    scheduler.tick()
    parts = eps()
    assert parts[2]["scheduled_at"] == pytest.approx(parts[1]["scheduled_at"] + 3600, abs=1)


def test_approval_required_or_automatic(series):
    make, eps, queued, fake = series
    pid = make({"require_approval": True, "window_start": "00:00", "window_end": "00:00"}, n=1, approved=0)
    autopublish.apply(pid)
    e = eps()[0]
    db.update("episodes", e["id"], {"scheduled_at": db.now() - 1})
    scheduler.tick()
    assert not queued and "одобрения" in db.get("episodes", e["id"])["publish_info"]["reminder"]
    p = db.get("projects", pid)
    p["settings"]["autopublish"]["require_approval"] = False
    db.update("projects", pid, {"settings": p["settings"]})
    scheduler.tick()
    assert queued and db.get("episodes", e["id"])["approved"] == 1


def test_waits_for_video_and_tiktok_connection(series):
    make, eps, queued, fake = series
    pid = make({"window_start": "00:00", "window_end": "00:00"}, n=1, status="rendering")
    autopublish.apply(pid)
    e = eps()[0]
    db.update("episodes", e["id"], {"scheduled_at": db.now() - 1})
    scheduler.tick()
    assert not queued and "видео" in db.get("episodes", e["id"])["publish_info"]["reminder"]
    db.update("episodes", e["id"], {"status": "ready"})
    fake.connected = False
    scheduler.tick()
    assert not queued and "не подключён" in db.get("episodes", e["id"])["publish_info"]["reminder"]


def test_failed_part_is_retried_then_pauses_series(series):
    make, eps, queued, fake = series
    pid = make({"window_start": "00:00", "window_end": "00:00"}, n=2)
    autopublish.apply(pid)
    first, second = eps()
    info = dict(first["publish_info"], error="TikTok отклонил публикацию")
    db.update("episodes", first["id"], {"publish_status": "failed", "publish_info": info})
    now = db.now()
    autopublish.retry_failed(db.get("projects", pid), now)
    e = db.get("episodes", first["id"])
    assert e["publish_status"] == "scheduled" and e["publish_info"]["attempts"] == 1
    assert e["scheduled_at"] == pytest.approx(now + 15 * 60, abs=1)
    info = dict(e["publish_info"], attempts=autopublish.MAX_ATTEMPTS)
    db.update("episodes", first["id"], {"publish_status": "failed", "publish_info": info})
    db.update("episodes", second["id"], {"scheduled_at": db.now() - 1})
    scheduler.tick()
    assert db.get("episodes", first["id"])["publish_status"] == "failed"
    assert "на паузе" in db.get("episodes", second["id"])["publish_info"]["reminder"]
    assert not queued


def test_stuck_publishing_status_is_refreshed(series):
    make, eps, queued, fake = series
    pid = make({"window_start": "00:00", "window_end": "00:00"}, n=1)
    autopublish.apply(pid)
    e = eps()[0]
    info = dict(e["publish_info"], publish_id="p1", checked_at=db.now() - 600)
    db.update("episodes", e["id"], {"publish_status": "publishing", "publish_info": info})
    scheduler.tick()
    assert db.get("episodes", e["id"])["publish_status"] == "published"


def test_api_requires_tiktok_and_stop_restores_manual(client, monkeypatch):
    fake = FakeClient(connected=False)
    monkeypatch.setattr(pipeline, "tiktok_client", lambda: fake)
    pid = db.new_id()
    t = db.now()
    db.insert("projects", {"id": pid, "title": "API", "created_at": t, "updated_at": t, "settings": {}})
    for i in range(2):
        db.insert("episodes", {"id": db.new_id(), "project_id": pid, "idx": i, "start_frame": i, "end_frame": i + 1,
                               "start_time": i, "end_time": i + 1, "duration": 1, "status": "ready", "updated_at": t})
    body = {"enabled": True, "interval_minutes": 90, "window_start": "00:00", "window_end": "00:00"}
    assert client.put(f"/api/projects/{pid}/autopublish", json=body).status_code == 400
    fake.connected = True
    r = client.put(f"/api/projects/{pid}/autopublish", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["tiktok"]["privacy_options"] == ["SELF_ONLY", "PUBLIC_TO_EVERYONE"]
    a, b = d["parts"]
    assert a["auto"] and b["scheduled_at"] - a["scheduled_at"] == pytest.approx(5400, abs=1)
    # Saving other project settings keeps the automatic publication settings.
    r = client.patch(f"/api/projects/{pid}", json={"settings": {"episode_seconds": 60}})
    assert r.status_code == 200 and r.json()["settings"]["autopublish"]["interval_minutes"] == 90
    d = client.put(f"/api/projects/{pid}/autopublish", json={**body, "enabled": False}).json()
    assert all(not p["auto"] and p["scheduled_at"] is None and p["publish_status"] == "not_published"
               for p in d["parts"])
    assert client.put(f"/api/projects/{pid}/autopublish", json={**body, "window_start": "25:00"}).status_code == 422
