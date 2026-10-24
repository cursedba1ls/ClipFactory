"""Release flow without an approval step: conversion survives a failed speech recognition, parts can be
deleted / restored, cancel never leaves a project stuck, "Выпустить в TikTok" uploads through the API."""
import time

import httpx
import pytest

from backend.database import db
from backend.errors import UserError
from backend.jobs import pipeline
from backend.jobs.queue import manager
from backend.services import publisher, secure_store
from tests.conftest import make_gameplay, make_source
from tests.test_pipeline import settings, upload, wait_job
from tests.test_publish import FakeTikTok


def broken_transcribe(*a, **k):
    raise UserError("Не удалось загрузить модель распознавания речи «small».", detail="no internet")


@pytest.fixture(scope="module")
def series(client):
    original = pipeline.TRANSCRIBE
    pipeline.TRANSCRIBE = broken_transcribe
    try:
        main = upload(client, make_source("release_main.mp4", 30), "main")
        gp = upload(client, make_gameplay("release_gp.mp4", duration=10), "gameplay")
        r = client.post("/api/projects", json={"title": "Выпуск", "main_media_id": main["id"],
                                               "gameplay_media_ids": [gp["id"]],
                                               "settings": settings(episode_seconds=10), "start": True})
        assert r.status_code == 200, r.text
        job = wait_job(client, r.json()["job"]["id"])
        yield {"job": job, "id": r.json()["id"]}
    finally:
        pipeline.TRANSCRIBE = original


def test_failed_recognition_does_not_stop_conversion(series, client):
    assert series["job"]["status"] == "done", series["job"]
    p = client.get(f"/api/projects/{series['id']}").json()
    assert p["status"] == "ready" and p["counts"]["ready"] == 3
    assert all(e["subtitle_status"] == "empty" and e["video_url"] for e in p["episodes"])
    assert any("Субтитры не созданы" in w for w in p["warnings"])


def test_delete_and_restore_part(series, client):
    pid = series["id"]
    eps = client.get(f"/api/projects/{pid}").json()["episodes"]
    target = eps[1]
    assert client.delete(f"/api/episodes/{target['id']}").json() == {"ok": True}
    p = client.get(f"/api/projects/{pid}").json()
    assert [e["number"] for e in p["episodes"]] == [1, 3]
    assert p["episodes"][0]["total"] == 3  # the other parts keep "Часть N/3"
    assert p["deleted_parts"] == [{"id": target["id"], "number": 2}] and p["counts"]["total"] == 2
    assert all(i["id"] != target["id"] for i in client.get("/api/publish/queue").json())
    r = client.post(f"/api/publish/episodes/{target['id']}/tiktok", json={"mode": "direct"})
    assert r.status_code == 400 and "удалена" in r.json()["detail"]
    assert client.post(f"/api/episodes/{target['id']}/render").status_code == 400
    r = client.post(f"/api/episodes/{target['id']}/restore")
    assert r.status_code == 200 and r.json()["deleted"] is False
    assert len(client.get(f"/api/projects/{pid}").json()["episodes"]) == 3


def test_cancel_queued_build_settles_project(series, client):
    pid = series["id"]
    # Keep the render lane busy so the next build stays queued.
    blocker = client.post(f"/api/projects/{pid}/build", json={"rerender_all": True}).json()
    second = manager.enqueue("render_episodes", project_id=pid, payload={"episode_ids": []}, dedupe=False)
    r = client.post(f"/api/jobs/{second['id']}/cancel")
    assert r.json()["status"] == "cancelled"
    client.post(f"/api/jobs/{blocker['id']}/cancel")
    assert wait_job(client, blocker["id"], timeout=120)["status"] in ("cancelled", "done")
    p = db.get("projects", pid)
    assert p["status"] in ("cancelled", "ready")
    assert not db.query("episodes", "project_id = ? AND status = 'rendering'", (pid,))
    # parts that had a video keep showing it while they wait to be made again
    for e in client.get(f"/api/projects/{pid}").json()["episodes"]:
        assert e["status"] in ("ready", "outdated") and e["video_url"]
    retry = client.post(f"/api/projects/{pid}/build", json={}).json()
    assert wait_job(client, retry["id"])["status"] == "done"
    assert db.get("projects", pid)["status"] == "ready"


def test_release_to_tiktok_without_approval(series, client, monkeypatch):
    secure_store.set_secret("tiktok_client_secret", "secret-xyz")
    secure_store.set_secret("tiktok_tokens", {"access_token": "acc", "refresh_token": "ref",
                                              "expires_at": time.time() + 3600,
                                              "refresh_expires_at": time.time() + 86400})
    fake = FakeTikTok()
    tt = publisher.TikTokClient("key123", "http://127.0.0.1:8765/api/publish/tiktok/callback",
                                http=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(pipeline, "tiktok_client", lambda: tt)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    monkeypatch.setattr("backend.api.publish.tiktok_client", lambda: tt)
    try:
        ep = client.get(f"/api/projects/{series['id']}").json()["episodes"][0]
        assert ep["approved"] is False
        r = client.post(f"/api/publish/episodes/{ep['id']}/tiktok",
                        json={"mode": "direct", "privacy_level": "PUBLIC_TO_EVERYONE"})
        assert r.status_code == 200, r.text
        job = wait_job(client, r.json()["id"], timeout=120)
        assert job["status"] == "done", job
        e = db.get("episodes", ep["id"])
        assert e["publish_status"] == "published" and fake.uploaded > 0
        r = client.post(f"/api/publish/episodes/{ep['id']}/tiktok", json={"mode": "direct"})
        assert r.status_code == 400  # already published: not uploaded twice
    finally:
        secure_store.set_secret("tiktok_tokens", None)
