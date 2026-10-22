"""Virtual camera (scene cuts, pans, shot sizes), screen layouts, banner strip, YouTube search/fragments."""
import subprocess

import numpy as np
import pytest

from backend.database import db
from backend.jobs import pipeline
from backend.services import downloader, smart_crop, video_composer
from tests.conftest import MEDIA_DIR, ffmpeg, make_gameplay, make_source
from tests.test_pipeline import expected_luma, fake_transcribe, upload, wait_job


@pytest.fixture(scope="module", autouse=True)
def _fake_whisper():
    original = pipeline.TRANSCRIBE
    pipeline.TRANSCRIBE = fake_transcribe
    yield
    pipeline.TRANSCRIBE = original


# ---------------------------------------------------------------------------
# Screen layout
# ---------------------------------------------------------------------------

def test_regions_without_banner_match_layout():
    r = video_composer.screen_regions("60/40")
    assert r["main"] == (0, 1152) and r["game"] == (1152, 768) and r["banner"] is None
    assert video_composer.split_y(r) == 1152
    r = video_composer.screen_regions("60/40", gameplay_top=True)
    assert r["game"] == (0, 768) and r["main"] == (768, 1152)
    assert video_composer.split_y(r) == 768


@pytest.mark.parametrize("position,expected", [
    ("top", {"banner": (0, 300), "main": (300, 810), "game": (1110, 810)}),
    ("middle", {"main": (0, 810), "banner": (810, 300), "game": (1110, 810)}),
    ("bottom", {"main": (0, 810), "game": (810, 810), "banner": (1620, 300)}),
])
def test_reserved_banner_strip(position, expected):
    r = video_composer.screen_regions("50/50", {"enabled": True, "mode": "reserve", "position": position,
                                                "height": 300})
    for k, v in expected.items():
        assert r[k] == v
    assert sum(h for _, h in (r["main"], r["game"], r["banner"])) == 1920


def test_overlay_banner_keeps_pictures():
    r = video_composer.screen_regions("50/50", {"enabled": True, "mode": "overlay", "position": "middle",
                                                "height": 200})
    assert r["main"] == (0, 960) and r["game"] == (960, 960)
    assert r["banner"] == (860, 200)
    assert video_composer.screen_regions("50/50", {"enabled": False, "height": 200})["banner"] is None


# ---------------------------------------------------------------------------
# Camera expressions
# ---------------------------------------------------------------------------

def _eval_path(expr: str, n: int) -> float:
    """Evaluate a composer path expression in Python (same functions ffmpeg provides)."""
    import math

    py = expr.replace("PI", "math.pi")
    env = {"n": n, "math": math, "cos": math.cos, "lt": lambda a, b: 1.0 if a < b else 0.0,
           "clip": lambda v, a, b: max(a, min(b, v)), "if": None}
    py = py.replace("if(", "_if(")
    env["_if"] = lambda c, a, b: a if c else b
    # Python evaluates both branches eagerly, which is fine for these pure expressions.
    return eval(py, env)  # noqa: S307 — test-only, expression produced by our own code


def test_path_expr_holds_eases_and_cuts():
    keys = [(0, 0.2), (10, 0.2), (30, 0.8), (49, 0.8), (50, 0.1)]
    e = video_composer.path_expr(keys)
    assert _eval_path(e, 0) == pytest.approx(0.2)
    assert _eval_path(e, 10) == pytest.approx(0.2)
    assert _eval_path(e, 20) == pytest.approx(0.5, abs=0.01)  # half way, eased
    assert _eval_path(e, 30) == pytest.approx(0.8)
    assert _eval_path(e, 49) == pytest.approx(0.8)
    assert _eval_path(e, 50) == pytest.approx(0.1)  # hard cut
    assert _eval_path(e, 500) == pytest.approx(0.1)
    assert video_composer.path_expr([(0, 0.3)]) == "0.3"


def test_camera_runs_merge_same_zoom_and_hold_until_cut():
    cam = [
        {"f0": 0, "f1": 50, "zoom": 1.0, "keys": [[0, 0.2, 0.5], [25, 0.4, 0.5]]},
        {"f0": 50, "f1": 80, "zoom": 1.0, "keys": [[60, 0.9, 0.5]]},
        {"f0": 80, "f1": 120, "zoom": 1.3, "keys": [[80, 0.5, 0.4]]},
    ]
    runs = video_composer.camera_runs(cam)
    assert [(r["f0"], r["f1"], r["zoom"]) for r in runs] == [(0, 80, 1.0), (80, 120, 1.3)]
    assert runs[0]["keys"] == [[0, 0.2, 0.5], [25, 0.4, 0.5], [49, 0.4, 0.5], [50, 0.9, 0.5], [60, 0.9, 0.5]]
    assert runs[1]["keys"] == [[0, 0.5, 0.4]]  # relative to the run start


def _square_frames(n: int, x: int, w: int = 128, h: int = 72, level: int = 60, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    f = np.full((n, h, w), level, dtype=np.uint8)
    for i in range(n):
        xx = x + int(4 * np.sin(i / 3))
        f[i, 26:46, xx:xx + 20] = 230
    return np.clip(f.astype(np.int16) + rng.integers(-2, 3, f.shape), 0, 255).astype(np.uint8)


def test_detect_cuts_finds_shot_changes():
    frames = np.concatenate([_square_frames(60, 10, level=40), _square_frames(48, 90, level=150),
                             _square_frames(70, 50, level=90)])
    assert smart_crop.detect_cuts(frames, 24) == [60, 108]
    # Motion alone (no cut) is not a cut.
    assert smart_crop.detect_cuts(_square_frames(120, 50), 24) == []


def test_plan_camera_reframes_each_shot():
    frames = np.concatenate([_square_frames(72, 8), _square_frames(72, 100, level=120)])
    plan = smart_crop.plan_camera(frames, 24.0, 0.45, 1.0, style="follow")
    assert [(s["f0"], s["f1"]) for s in plan] == [(0, 72), (72, 144)]
    assert plan[0]["keys"][0][1] < 0.3 and plan[1]["keys"][0][1] > 0.7
    assert all(s["zoom"] == 1.0 for s in plan)


def test_plan_camera_dynamic_varies_shot_size():
    frames = np.concatenate([_square_frames(240, 30), _square_frames(240, 80, level=110)])
    plan = smart_crop.plan_camera(frames, 24.0, 0.6, 1.0, style="dynamic", seed=3)
    assert plan[0]["f0"] == 0 and plan[-1]["f1"] == len(frames)
    assert all(a["f1"] == b["f0"] for a, b in zip(plan, plan[1:]))
    zooms = [s["zoom"] for s in plan]
    assert len(set(zooms)) > 1, zooms
    assert all(1.0 <= z <= 1.36 for z in zooms)


# ---------------------------------------------------------------------------
# End to end: camera with zoom changes + banner + gameplay on top, frame exact
# ---------------------------------------------------------------------------

def _region_lumas(path: str, y: int, h: int) -> list[int]:
    vf = f"crop=40:40:520:{y + h // 2 - 20},scale=1:1:flags=area"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return list(raw)


def _rgb_at(path: str, x: int, y: int) -> tuple[int, int, int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", path, "-frames:v", "1",
                          "-vf", f"crop=8:8:{x}:{y},scale=1:1:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return tuple(raw[:3])


def test_dynamic_camera_and_banner_render_frame_exact(client):
    src = make_source("camera_src.mp4", 24, fps=10, size="320x180")  # luma jumps every 50 frames = shot cuts
    main = upload(client, src, "main")
    gp = upload(client, make_gameplay("gameplay.mp4", duration=20), "gameplay")
    png = MEDIA_DIR / "banner.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=0xFF0000:s=200x100", "-frames:v", "1", str(png))
    r = client.post("/api/banners?filename=banner.png", content=png.read_bytes())
    assert r.status_code == 200, r.text
    banner_id = r.json()["id"]
    assert client.get(f"/api/banners/{banner_id}.png").status_code == 200
    settings = {
        "episode_seconds": 12, "quality": "fast", "subtitles_enabled": False, "layout": "60/40",
        "gameplay_top": True, "crop": {"mode": "auto", "style": "dynamic"},
        "banner": {"enabled": True, "mode": "reserve", "position": "top", "height": 200, "background": "#0000FF",
                   "image_id": banner_id, "text": "Подпишись!"},
    }
    r = client.post("/api/projects", json={"main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
                                           "settings": settings})
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done", job
    p = client.get(f"/api/projects/{r.json()['id']}").json()
    regions = video_composer.screen_regions("60/40", settings["banner"], True)
    assert regions["banner"] == (0, 200) and regions["main"][0] > regions["game"][0]
    first = 0
    for e in p["episodes"]:
        row = db.get("episodes", e["id"])
        cam = row["auto_crop"]["camera"]
        assert cam and cam[0]["f0"] == 0
        path = row["video_path"]
        lumas = _region_lumas(path, *regions["main"])
        frames = row["end_frame"] - row["start_frame"]
        assert len(lumas) == frames
        for i, v in enumerate(lumas):
            assert abs(v - expected_luma(first + i)) <= 6, (e["idx"], i, v)
        first += frames
        # Banner strip: the red picture (scaled to 400×200, centred), the blue background at the edge.
        r_, g_, b_ = _rgb_at(path, 360, 14)
        assert r_ > 180 and g_ < 80 and b_ < 80, (r_, g_, b_)
        r_, g_, b_ = _rgb_at(path, 10, 10)
        assert b_ > 180 and r_ < 80, (r_, g_, b_)
    ass = (db.get("episodes", p["episodes"][0]["id"])["video_path"]).replace(".mp4", ".ass")
    text = open(ass, encoding="utf-8").read()
    assert "Подпишись!" in text and "Style: Banner" in text


def test_static_style_has_no_camera(client):
    src = make_source("camera_static.mp4", 12, fps=10, size="320x180")
    main = upload(client, src, "main")
    gp = upload(client, make_gameplay("gameplay.mp4", duration=20), "gameplay")
    r = client.post("/api/projects", json={"main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
                                           "settings": {"episode_seconds": 12, "quality": "fast",
                                                        "subtitles_enabled": False,
                                                        "crop": {"mode": "auto", "style": "static"}}})
    assert wait_job(client, r.json()["job"]["id"])["status"] == "done"
    e = client.get(f"/api/projects/{r.json()['id']}").json()["episodes"][0]
    assert db.get("episodes", e["id"])["auto_crop"].get("camera") is None


def test_banner_upload_rejects_non_images(client):
    r = client.post("/api/banners?filename=x.txt", content=b"hello")
    assert r.status_code == 400
    r = client.post("/api/banners?filename=x.png", content=b"not a png")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# YouTube search and fragment downloads (yt-dlp replaced by a fake)
# ---------------------------------------------------------------------------

class FakeYDL:
    calls: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        FakeYDL.calls.append((url, self.opts, download))
        if not download:
            return {"entries": [
                {"id": "aaaaaaaaaaa", "title": "Short cartoon", "duration": 300, "channel": "Studio"},
                {"id": "bbbbbbbbbbb", "title": "Tiny", "duration": 20, "channel": "X"},
                {"id": "ccccccccccc", "title": "Live", "live_status": "is_live"},
                None,
            ]}
        out = self.opts["outtmpl"].replace("%(ext)s", "mp4")
        open(out, "wb").write(b"\0" * 10)
        return {"title": "Gameplay"}


@pytest.fixture
def fake_ytdlp(monkeypatch):
    import yt_dlp
    FakeYDL.calls = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_search_creative_commons_by_default(client, fake_ytdlp):
    r = client.get("/api/media/search", params={"q": "animated short", "min_seconds": 60})
    assert r.status_code == 200, r.text
    res = r.json()["results"]
    assert [x["id"] for x in res] == ["aaaaaaaaaaa"]
    assert res[0]["url"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    url = fake_ytdlp.calls[0][0]
    assert "search_query=animated+short" in url and "sp=EgIwAQ%3D%3D" in url
    client.get("/api/media/search", params={"q": "roblox obby", "cc": "false"})
    assert fake_ytdlp.calls[-1][0].startswith("ytsearch")
    assert client.get("/api/media/search", params={"q": " "}).status_code == 400


def test_fragment_download_passes_ranges(tmp_path, fake_ytdlp):
    path, title = downloader.download_url("https://www.youtube.com/watch?v=aaaaaaaaaaa", tmp_path, "abc",
                                          start=30, end=150)
    assert path.name == "abc.mp4" and title == "Gameplay"
    opts = fake_ytdlp.calls[-1][1]
    assert opts["force_keyframes_at_cuts"] is True
    ranges = list(opts["download_ranges"]({}, None))
    assert ranges[0]["start_time"] == 30 and ranges[0]["end_time"] == 150


def test_url_import_keeps_fragment_separately(client, monkeypatch):
    from backend.jobs.queue import manager
    monkeypatch.setattr(manager, "enqueue", lambda *a, **k: None)
    url = "https://example.com/video.mp4"
    a = client.post("/api/media/url", json={"url": url, "kind": "gameplay", "start": 10, "end": 70}).json()
    b = client.post("/api/media/url", json={"url": url, "kind": "gameplay"}).json()
    c = client.post("/api/media/url", json={"url": url, "kind": "gameplay", "start": 10, "end": 70}).json()
    assert a["id"] != b["id"] and a["id"] == c["id"]
    m = db.get("media", a["id"])
    assert (m["fetch_start"], m["fetch_end"]) == (10, 70)
    r = client.post("/api/media/url", json={"url": url, "kind": "gameplay", "start": 10, "end": 11})
    assert r.status_code == 400
