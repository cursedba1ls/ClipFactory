"""Automatic and manual cropping: black bars, action tracking, manual override, crop editor frames."""
import subprocess

import numpy as np
import pytest

from backend.database import db
from backend.jobs import pipeline
from backend.services import renderer, smart_crop
from tests.conftest import MEDIA_DIR, ffmpeg, make_gameplay
from tests.test_pipeline import fake_transcribe, upload, wait_job


def moving_square(name: str, x0: int, duration: float = 8, letterbox: bool = False):
    """Grey 16:9 scene with a bright moving square around x0 (the 'action')."""
    out = MEDIA_DIR / name
    if out.exists():
        return out
    src = f"color=c=0x404040:s=1280:{540 if letterbox else 720}:r=24:d={duration}"
    vf = f"drawbox=x={x0}+40*sin(t*3):y=ih/2-60+30*cos(t*2):w=120:h=120:color=white:t=fill"
    if letterbox:
        vf += ",pad=1280:720:0:90:black"
    ffmpeg("-f", "lavfi", "-i", src.replace("1280:", "1280x"), "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", str(out))
    return out


def test_letterbox_detected():
    src = moving_square("letterbox.mp4", 600, letterbox=True)
    box = smart_crop.detect_content_box(src, 8, 1280, 720)
    assert box is not None
    assert box["y"] == pytest.approx(90, abs=4) and box["h"] == pytest.approx(540, abs=8)
    assert box["w"] == pytest.approx(1280, abs=8)


def test_full_frame_has_no_box():
    src = moving_square("square_mid.mp4", 580)
    assert smart_crop.detect_content_box(src, 8, 1280, 720) is None


@pytest.mark.parametrize("x0,check", [(1050, lambda x: x > 0.75), (60, lambda x: x < 0.25), (580, lambda x: 0.35 < x < 0.65)])
def test_focus_follows_action(x0, check):
    src = moving_square(f"square_{x0}.mp4", x0)
    f = smart_crop.focus(src, 0, 8, 1280, 720, 1080, 960)
    assert check(f["x"]), f


def test_flat_picture_stays_centred():
    out = MEDIA_DIR / "flat.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=0x305070:s=1280x720:r=24:d=4", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", str(out))
    assert smart_crop.focus(out, 0, 4, 1280, 720, 1080, 960) == {"x": 0.5, "y": 0.5}


def test_window_fractions():
    assert smart_crop.window_fractions(1920, 1080, 1080, 960) == pytest.approx((0.6328, 1.0), abs=1e-3)
    wf, hf = smart_crop.window_fractions(1920, 1080, 1080, 960, zoom=2)
    assert wf == pytest.approx(0.3164, abs=1e-3) and hf == 0.5


def test_resolve_crop_auto_and_manual():
    ep = {"auto_crop": {"main_x": 0.83, "main_y": 0.4}}
    gp = {"info": {"focus": {"x": 0.3, "y": 0.6}}}
    auto = renderer.resolve_crop({"crop": {"mode": "auto", "main_zoom": 1.2}}, ep, gp)
    assert (auto["main_x"], auto["main_y"], auto["game_x"], auto["game_y"], auto["main_zoom"]) == (0.83, 0.4, 0.3, 0.6, 1.2)
    manual = renderer.resolve_crop({"crop": {"mode": "manual", "main_x": 0.1, "game_x": 0.9}}, ep, gp)
    assert (manual["main_x"], manual["game_x"]) == (0.1, 0.9)


@pytest.fixture(scope="module", autouse=True)
def _fake_whisper():
    original = pipeline.TRANSCRIBE
    pipeline.TRANSCRIBE = fake_transcribe
    yield
    pipeline.TRANSCRIBE = original


def top_band_luma(path: str, top_h: int = 960) -> np.ndarray:
    """Mean luma per row of the top half (first frame)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-frames:v", "1", "-vf", f"crop=1080:{top_h}:0:0",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(top_h, 1080).mean(axis=1)


def test_auto_crop_end_to_end_removes_bars_and_tracks_action(client):
    src = moving_square("letterbox_right.mp4", 1000, duration=10, letterbox=True)
    main = upload(client, src, "main")
    assert main["info"]["content_box"]["h"] == pytest.approx(540, abs=8)
    gp = upload(client, make_gameplay("gameplay.mp4", duration=20), "gameplay")
    assert "focus" in gp["info"]
    r = client.post("/api/projects", json={"main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
                                           "settings": {"episode_seconds": 5, "quality": "fast",
                                                        "subtitles_enabled": False}})
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done", job
    p = client.get(f"/api/projects/{r.json()['id']}").json()
    assert len(p["episodes"]) == 2
    for e in p["episodes"]:
        assert e["auto_crop"]["main_x"] > 0.7  # the action is on the right
        rows = top_band_luma(db.get("episodes", e["id"])["video_path"])
        assert rows[:950].min() > 30, "letterbox bars must not reach the output (rows 957+ are the divider)"
    # crop editor data for an episode
    ep = client.get(f"/api/episodes/{p['episodes'][0]['id']}").json()
    assert ep["effective"]["crop_resolved"]["main_x"] > 0.7
    frame = client.get(ep["crop_sources"]["main"]["url"])
    assert frame.status_code == 200 and frame.content[:2] == b"\xff\xd8"
    assert ep["crop_sources"]["main"]["dims"]["h"] == pytest.approx(540, abs=8)
    # manual override for one part
    r = client.patch(f"/api/episodes/{ep['id']}", json={
        "overrides": {"crop": {"mode": "manual", "main_x": 0.0, "main_zoom": 1.5}}, "render": True})
    assert wait_job(client, r.json()["job"]["id"])["status"] == "done"
    ep2 = client.get(f"/api/episodes/{ep['id']}").json()
    assert ep2["effective"]["crop_resolved"]["main_x"] == 0.0
    assert ep2["effective"]["crop_resolved"]["main_zoom"] == 1.5
