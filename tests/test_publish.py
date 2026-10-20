"""Publication: TikTok API client (mocked HTTP), status handling, captions, scheduler."""
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from backend.database import db
from backend.errors import UserError
from backend.services import captions, publisher, secure_store
from backend.services.publisher import MB, TikTokClient, TikTokError, chunk_plan, interpret_status


def test_chunk_plan_follows_tiktok_rules():
    assert chunk_plan(3 * MB) == (3 * MB, 1)
    assert chunk_plan(64 * MB) == (64 * MB, 1)
    size = 100 * MB + 123
    cs, n = chunk_plan(size)
    assert cs == 10 * MB and n == 10
    assert size - cs * (n - 1) <= 128 * MB  # last chunk absorbs the remainder
    with pytest.raises(UserError):
        chunk_plan(0)


@pytest.mark.parametrize("raw,expected", [
    ({"status": "PUBLISH_COMPLETE"}, ("published", True)),
    ({"status": "SEND_TO_USER_INBOX"}, ("inbox", True)),
    ({"status": "FAILED", "fail_reason": "file_format_check_failed"}, ("failed", True)),
    ({"status": "PROCESSING_UPLOAD"}, ("publishing", False)),
    ({}, ("publishing", False)),
])
def test_status_interpretation(raw, expected):
    status, msg, final = interpret_status(raw, "direct")
    assert (status, final) == expected
    assert msg  # always a Russian message


class FakeTikTok:
    def __init__(self, statuses=("PROCESSING_UPLOAD", "PUBLISH_COMPLETE"), init_error=None):
        self.calls = []
        self.uploaded = 0
        self.statuses = list(statuses)
        self.init_error = init_error

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        if url == publisher.TOKEN_URL:
            form = parse_qs(request.content.decode())
            assert form["client_secret"] == ["secret-xyz"]
            return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh",
                                             "expires_in": 86400, "refresh_expires_in": 86400 * 365,
                                             "open_id": "u1", "scope": publisher.SCOPES})
        if url == "https://upload.test/u":
            assert "Authorization" not in request.headers  # upload URL is pre-signed
            assert request.headers["Content-Range"].startswith("bytes 0-")
            self.uploaded += len(request.content)
            return httpx.Response(201)
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        if url == publisher.CREATOR_INFO_URL:
            return httpx.Response(200, json={"data": {"privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
                                                      "max_video_post_duration_sec": 600},
                                             "error": {"code": "ok"}})
        if url in (publisher.DIRECT_INIT_URL, publisher.INBOX_INIT_URL):
            if self.init_error:
                return httpx.Response(400, json={"error": {"code": self.init_error, "message": "x", "log_id": "1"}})
            body = json.loads(request.content)
            assert body["source_info"]["source"] == "FILE_UPLOAD"
            if url == publisher.DIRECT_INIT_URL:
                assert body["post_info"]["privacy_level"] in ("SELF_ONLY", "PUBLIC_TO_EVERYONE")
            return httpx.Response(200, json={"data": {"publish_id": "p_123", "upload_url": "https://upload.test/u"},
                                             "error": {"code": "ok"}})
        if url == publisher.STATUS_URL:
            st = self.statuses.pop(0) if self.statuses else "PUBLISH_COMPLETE"
            return httpx.Response(200, json={"data": {"status": st}, "error": {"code": "ok"}})
        return httpx.Response(404)


@pytest.fixture()
def tiktok(tmp_path):
    secure_store.set_secret("tiktok_client_secret", "secret-xyz")
    secure_store.set_secret("tiktok_tokens", {"access_token": "acc", "refresh_token": "ref",
                                              "expires_at": time.time() + 3600,
                                              "refresh_expires_at": time.time() + 86400})
    fake = FakeTikTok()
    client = TikTokClient("key123", "http://127.0.0.1:8765/api/publish/tiktok/callback",
                          http=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 2048)
    yield client, fake, video
    secure_store.set_secret("tiktok_tokens", None)


def test_direct_publish_flow_confirms_status(tiktok):
    client, fake, video = tiktok
    pid = client.publish_video(video, "Часть 1/3 #тест", mode="direct", privacy_level="SELF_ONLY", duration=60)
    assert pid == "p_123" and fake.uploaded == 2048
    s1 = interpret_status(client.fetch_status(pid), "direct")
    assert s1[0] == "publishing" and not s1[2]
    s2 = interpret_status(client.fetch_status(pid), "direct")
    assert s2[0] == "published" and s2[2]


def test_inbox_mode_does_not_send_post_info(tiktok):
    client, fake, video = tiktok
    client.publish_video(video, "caption", mode="inbox")
    assert any(u == publisher.INBOX_INIT_URL for _, u in fake.calls)
    assert not any(u == publisher.CREATOR_INFO_URL for _, u in fake.calls)


def test_privacy_level_must_be_allowed(tiktok):
    client, _, video = tiktok
    with pytest.raises(UserError) as exc:
        client.publish_video(video, "c", mode="direct", privacy_level="FOLLOWER_OF_CREATOR")
    assert "приватности" in exc.value.message


def test_api_errors_are_translated(tiktok):
    client, fake, video = tiktok
    fake.init_error = "unaudited_client_can_only_post_to_private_accounts"
    with pytest.raises(TikTokError) as exc:
        client.publish_video(video, "c", mode="direct")
    assert "аудит" in exc.value.message


def test_expired_token_is_refreshed(tiktok):
    client, fake, _ = tiktok
    tok = secure_store.get_secret("tiktok_tokens")
    tok["expires_at"] = time.time() - 10
    secure_store.set_secret("tiktok_tokens", tok)
    assert client.access_token() == "new-access"
    assert any(u == publisher.TOKEN_URL for _, u in fake.calls)
    assert secure_store.get_secret("tiktok_tokens")["refresh_token"] == "new-refresh"


def test_oauth_url_and_state_validation(tiktok):
    client, _, _ = tiktok
    url = client.build_auth_url()
    q = parse_qs(urlsplit(url).query)
    assert url.startswith(publisher.AUTH_URL)
    assert q["client_key"] == ["key123"] and q["response_type"] == ["code"]
    assert "video.publish" in q["scope"][0] and q["code_challenge_method"] == ["S256"]
    with pytest.raises(UserError):
        client.exchange_code("code", "forged-state")
    client.exchange_code("code", q["state"][0])
    assert secure_store.get_secret("tiktok_tokens")["access_token"] == "new-access"


def test_rate_limiter_waits():
    lim = publisher.RateLimiter(2, 0.3)
    t = time.monotonic()
    for _ in range(3):
        lim.wait()
    assert time.monotonic() - t >= 0.25


def test_secrets_not_stored_in_plain_settings(client):
    r = client.post("/api/publish/tiktok/config", json={"client_key": "k", "client_secret": "super-secret",
                                                         "redirect_uri": "http://127.0.0.1:8765/cb"})
    assert r.status_code == 200
    assert "super-secret" not in json.dumps(client.get("/api/settings").json())
    assert "super-secret" not in r.text
    raw = db.scalar("SELECT value FROM settings WHERE key = 'app'")
    assert "super-secret" not in raw
    secure_store.set_secret("tiktok_client_secret", None)


def test_captions_neutral_and_numbered():
    assert captions.make_caption(1, 5) == "Начало истории 🔥 | Часть 1/5"
    assert captions.make_caption(5, 5) == "Финальная часть 😳 | Часть 5/5"
    assert captions.make_caption(2, 5, "как я Лысым стал") == "как я Лысым стал часть 2"
    tags = captions.make_hashtags("#мультик roblox", "#кот", "собака собака собака бегает")
    assert tags.split()[:3] == ["#кот", "#мультик", "#roblox"]
    assert "#собака" in tags


def _make_episode(project_id: str, idx: int, **kw) -> dict:
    vals = {"id": db.new_id(), "project_id": project_id, "idx": idx, "start_frame": idx * 10,
            "end_frame": idx * 10 + 10, "start_time": idx, "end_time": idx + 1, "duration": 1,
            "status": "ready", "updated_at": db.now()}
    vals.update(kw)
    return db.insert("episodes", vals)


def test_scheduler_never_publishes_unapproved_and_reminds_manual():
    from backend.jobs import pipeline  # noqa: F401
    from backend.jobs.scheduler import tick
    pid = db.new_id()
    db.insert("projects", {"id": pid, "title": "sched", "created_at": db.now(), "updated_at": db.now()})
    past = db.now() - 60
    unapproved = _make_episode(pid, 0, scheduled_at=past, publish_status="scheduled", publish_mode="api_direct")
    manual = _make_episode(pid, 1, scheduled_at=past, publish_status="scheduled", approved=1)
    tick()
    u = db.get("episodes", unapproved["id"])
    m = db.get("episodes", manual["id"])
    assert u["publish_status"] == "scheduled" and "не одобрена" in u["publish_info"]["reminder"]
    assert m["publish_status"] == "scheduled" and "вручную" in m["publish_info"]["reminder"]
    assert not db.query("jobs", "type = 'tiktok_publish' AND episode_id IN (?, ?)", (u["id"], m["id"]))


def test_manual_publication_marking(client):
    pid = db.new_id()
    db.insert("projects", {"id": pid, "title": "manual", "created_at": db.now(), "updated_at": db.now()})
    ep = _make_episode(pid, 0)
    r = client.post(f"/api/episodes/{ep['id']}/mark-published", json={"url": "https://www.tiktok.com/@me/video/1"})
    assert r.json()["publish_status"] == "manual"
    r = client.post(f"/api/episodes/{ep['id']}/mark-published", json={"published": False})
    assert r.json()["publish_status"] == "not_published"
    q = client.get("/api/publish/queue").json()
    assert any(i["id"] == ep["id"] for i in q)


def test_queue_warns_about_wrong_order(client):
    pid = db.new_id()
    db.insert("projects", {"id": pid, "title": "order", "created_at": db.now(), "updated_at": db.now()})
    e1 = _make_episode(pid, 0, scheduled_at=db.now() + 7200)
    e2 = _make_episode(pid, 1, scheduled_at=db.now() + 3600)
    q = {i["id"]: i for i in client.get("/api/publish/queue").json()}
    assert "раньше" in q[e2["id"]].get("order_warning", "")
    assert "order_warning" not in q[e1["id"]]
