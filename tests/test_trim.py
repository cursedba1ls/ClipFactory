"""Trimming a long gameplay clip: the chosen fragment is the only part the parts may use."""
import pytest

from backend.database import db
from backend.services import gameplay_manager
from tests.conftest import make_gameplay, make_source
from tests.test_pipeline import settings, upload, wait_job


def test_range_is_intersection_of_black_edges_and_trim():
    media = {"info": {"video_duration": 600}, "usable_start": 1.0, "usable_end": 599.0,
             "trim_start": 100.0, "trim_end": 130.0}
    assert gameplay_manager.gameplay_range(media) == (100.0, 130.0)


def test_trim_outside_usable_area_is_ignored():
    # trim lies entirely in the black tail: only black frames would remain, so the trim is not applied
    media = {"info": {"video_duration": 600}, "usable_start": 0.0, "usable_end": 100.0,
             "trim_start": 200.0, "trim_end": 210.0}
    assert gameplay_manager.gameplay_range(media) == (0.0, 100.0)


def test_no_trim_uses_whole_usable_clip():
    media = {"info": {"video_duration": 40.0}, "usable_start": 0.0, "usable_end": None}
    assert gameplay_manager.gameplay_range(media) == (0.0, 40.0)


@pytest.fixture()
def clip(client):
    gp = upload(client, make_gameplay("trim_source.mp4", duration=20), "gameplay")
    assert gp["info"]["video_duration"] == pytest.approx(20, abs=0.1)
    return gp


def test_trim_api_validation_and_reset(client, clip):
    url = f"/api/media/{clip['id']}"
    r = client.patch(url, json={"trim_start": 5, "trim_end": 15})
    assert r.status_code == 200
    assert r.json()["usable_range"] == [5.0, 15.0]
    assert r.json()["trim_start"] == 5.0
    too_long = client.patch(url, json={"trim_end": 99})
    assert too_long.status_code == 400 and "дальше длины" in too_long.json()["detail"]
    too_short = client.patch(url, json={"trim_start": 14, "trim_end": 15})
    assert too_short.status_code == 400 and "минимум" in too_short.json()["detail"]
    back = client.patch(url, json={"reset_trim": True})
    assert back.json()["usable_range"][1] == pytest.approx(20, abs=0.1)
    assert back.json()["trim_start"] is None


def test_trim_marks_episodes_that_used_the_clip(client):
    main = upload(client, make_source("trim_main.mp4", 12), "main")
    gp = upload(client, make_gameplay("trim_used.mp4", duration=20), "gameplay")
    r = client.post("/api/projects", json={"main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
                                           "settings": settings(episode_seconds=6, subtitles_enabled=False)})
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done", job
    pid = r.json()["id"]
    before = client.get(f"/api/projects/{pid}").json()["episodes"]
    assert all(e["status"] == "ready" for e in before)
    client.patch(f"/api/media/{gp['id']}", json={"trim_start": 2, "trim_end": 9})
    after = client.get(f"/api/projects/{pid}").json()["episodes"]
    assert all(e["status"] == "outdated" for e in after)
    assert all(e["gameplay"] is None for e in after)  # re-chosen from the trimmed range on the next build
    # the next build renders from the trimmed fragment only
    j = client.post(f"/api/projects/{pid}/build", json={}).json()
    assert wait_job(client, j["id"])["status"] == "done"
    final = client.get(f"/api/projects/{pid}").json()["episodes"]
    assert all(e["status"] == "ready" for e in final)
    for e in final:
        assert e["gameplay"]["media_id"] == gp["id"]
        # offset counts from the start of the usable range, which is the trimmed start (2 s)
        assert 0.0 <= e["gameplay"]["offset"] < 7.0 + 0.01
