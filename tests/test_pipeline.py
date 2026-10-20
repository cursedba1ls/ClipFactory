"""End-to-end series production through the HTTP API with real ffmpeg rendering.

Speech recognition is replaced by a deterministic fake (the real faster-whisper
model needs a one-time download); everything else is the production code path.
"""
import json
import subprocess
import time
import zipfile
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest

from backend.database import db
from backend.jobs import pipeline
from backend.jobs.queue import manager
from tests.conftest import MEDIA_DIR, ffmpeg, make_gameplay, make_source

TRANSCRIBE_CALLS: list[str] = []


def fake_transcribe(audio_path, model_size="small", language=None, device="auto", on_progress=None,
                    cancel_event=None, duration=None):
    assert Path(audio_path).is_file()
    TRANSCRIBE_CALLS.append(str(audio_path))
    words, t, i = [], 0.3, 0
    while t < (duration or 0) - 0.4:
        words.append({"start": round(t, 3), "end": round(t + 0.35, 3), "text": f"слово{i}", "p": 1.0})
        t += 0.5
        i += 1
    if on_progress:
        on_progress(1.0)
    return {"language": "ru", "model": "fake", "words": words, "segments": []}


@pytest.fixture(scope="module", autouse=True)
def _fake_whisper():
    original = pipeline.TRANSCRIBE
    pipeline.TRANSCRIBE = fake_transcribe
    yield
    pipeline.TRANSCRIBE = original


def upload(client, path: Path, kind: str) -> dict:
    r = client.post(f"/api/media/upload?kind={kind}&filename={path.name}", content=path.read_bytes(),
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    return r.json()


def wait_job(client, job_id: str, timeout: float = 900) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.5)
    raise AssertionError("job timeout")


def ffprobe(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                         capture_output=True, check=True).stdout
    return json.loads(out)


def frame_lumas(path: str) -> list[int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "crop=40:40:20:20,scale=1:1:flags=area",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return list(raw)


def expected_luma(frame_index: int) -> int:
    y = 16 + (frame_index % 50) * 4  # limited-range luma written by the generator
    return max(0, min(255, round((y - 16) * 255 / 219)))  # read back as full-range gray


def audio_samples(path: str) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def settings(**kw):
    s = {"episode_seconds": 60, "quality": "fast", "layout": "50/50",
         "subtitle_style": {"preset": "classic"}, "numbering": {"enabled": True, "format": "Часть {n}/{total}"}}
    s.update(kw)
    return s


@pytest.fixture(scope="module")
def series_5min(client):
    TRANSCRIBE_CALLS.clear()
    main = upload(client, make_source("main_300.mp4", 300), "main")
    gp = upload(client, make_gameplay("gp_black.mp4", duration=12, black_head=1.0), "gameplay")
    assert gp["usable_start"] == pytest.approx(1.0, abs=0.05)
    r = client.post("/api/projects", json={"title": "Тест 5 минут", "main_media_id": main["id"],
                                           "gameplay_media_ids": [gp["id"]],
                                           "settings": settings(main_audio=True)})
    assert r.status_code == 200, r.text
    project = r.json()
    job = wait_job(client, project["job"]["id"])
    assert job["status"] == "done", job
    return client.get(f"/api/projects/{project['id']}").json()


def test_five_minute_source_gives_exactly_five_60s_episodes(series_5min):
    p = series_5min
    eps = p["episodes"]
    assert len(eps) == 5
    assert p["status"] == "ready"
    assert all(e["status"] == "ready" for e in eps)
    for e in eps:
        info = ffprobe(str(Path(db.get("episodes", e["id"])["video_path"])))
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = next(s for s in info["streams"] if s["codec_type"] == "audio")
        assert (v["width"], v["height"]) == (1080, 1920)
        assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
        assert a["codec_name"] == "aac" and int(a["sample_rate"]) == 48000 and a["channels"] == 2
        assert int(v["nb_frames"]) == 600
        assert float(v["duration"]) == pytest.approx(60.0, abs=0.01)
        assert float(a["duration"]) == pytest.approx(60.0, abs=0.05)


def test_every_source_frame_preserved_in_order(series_5min):
    """Each output frame's top half carries the source frame number: no gaps, no duplicates, no reordering."""
    for e in series_5min["episodes"]:
        path = db.get("episodes", e["id"])["video_path"]
        lumas = frame_lumas(path)
        expected = [expected_luma(i) for i in range(e["start_frame"], e["end_frame"])]
        assert len(lumas) == len(expected)
        bad = [k for k, (got, exp) in enumerate(zip(lumas, expected)) if abs(got - exp) > 4]
        assert not bad, f"episode {e['number']}: mismatched frames {bad[:10]}"


def test_audio_video_sync(series_5min):
    """Beeps occur every 7 s of source time; each must start at the same local time in its episode."""
    checked = 0
    for e in series_5min["episodes"]:
        x = audio_samples(db.get("episodes", e["id"])["video_path"])
        for k in range(1, 50):
            t = 7 * k
            if not (e["start_time"] + 0.1 <= t <= e["end_time"] - 0.3):
                continue
            local = t - e["start_time"]
            lo = int((local - 0.05) * 48000)
            seg = np.abs(x[lo: lo + int(0.2 * 48000)])
            onset = lo + int(np.argmax(seg > 0.3))
            assert abs(onset / 48000 - local) < 0.01, f"beep at {t}s drifted in episode {e['number']}"
            checked += 1
    assert checked >= 40


def test_subtitles_numbering_and_single_transcription(series_5min, client):
    assert len(TRANSCRIBE_CALLS) == 1  # whole video transcribed once
    e2 = client.get(f"/api/episodes/{series_5min['episodes'][1]['id']}").json()
    assert e2["subtitle_status"] == "auto" and e2["cues"]
    assert e2["cues"][0]["start"] >= 0
    ass = client.get(e2["ass_url"]).text
    assert "Часть 2/5" in ass and "слово" in ass
    srt = client.get(e2["srt_url"]).text
    assert "-->" in srt
    # first subtitle of episode 2 is the first word spoken after 60 s
    assert e2["cues"][0]["words"][0]["text"] == "слово120"


def test_edit_and_regenerate_single_episode(series_5min, client):
    ep = series_5min["episodes"][2]
    before = {e["id"]: db.get("episodes", e["id"])["rendered_at"] for e in series_5min["episodes"]}
    cues = [{"start": 1.0, "end": 3.0, "text": "Отредактированный текст", "words": []}]
    r = client.patch(f"/api/episodes/{ep['id']}", json={
        "cues": cues, "overrides": {"subtitle_style": {"preset": "karaoke", "font_size": 90},
                                    "crop": {"main_x": 0.2}}, "render": True})
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done", job
    after = client.get(f"/api/episodes/{ep['id']}").json()
    assert after["status"] == "ready" and after["subtitle_status"] == "edited"
    assert after["approved"] is False
    assert "Отредактированный" in client.get(after["ass_url"]).text
    assert db.get("episodes", ep["id"])["rendered_at"] > before[ep["id"]]
    for e in series_5min["episodes"]:
        if e["id"] != ep["id"]:
            assert db.get("episodes", e["id"])["rendered_at"] == before[e["id"]]
    assert len(TRANSCRIBE_CALLS) == 1  # no re-transcription for edits


def test_replace_gameplay_without_retranscribing(series_5min, client):
    gp2 = upload(client, make_gameplay("gp_second.mp4", duration=25), "gameplay")
    ep = series_5min["episodes"][0]
    r = client.patch(f"/api/episodes/{ep['id']}", json={"gameplay": {"media_id": gp2["id"], "offset": 3},
                                                        "render": True})
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done", job
    e = client.get(f"/api/episodes/{ep['id']}").json()
    assert e["gameplay"]["media_id"] == gp2["id"] and e["status"] == "ready"
    assert len(TRANSCRIBE_CALLS) == 1


def test_approval_and_download(series_5min, client):
    ep = series_5min["episodes"][3]
    r = client.post(f"/api/publish/episodes/{ep['id']}/tiktok", json={"mode": "direct"})
    assert r.status_code == 400 and "одобр" in r.json()["detail"]
    r = client.post(f"/api/episodes/{ep['id']}/approve", json={"approved": True})
    assert r.json()["approved"] is True
    d = client.get(ep["download_url"])
    assert d.status_code == 200 and d.content[4:8] == b"ftyp"
    z = client.get(f"/api/projects/{series_5min['id']}/export")
    assert z.status_code == 200
    names = zipfile.ZipFile(BytesIO(z.content)).namelist()
    assert sum(n.endswith(".mp4") for n in names) == 5 and "Подписи.txt" in names
    # captions were generated for every episode
    p = client.get(f"/api/projects/{series_5min['id']}").json()
    assert all("Часть" in e["caption"] for e in p["episodes"])
    assert p["episodes"][-1]["caption"].endswith("Часть 5/5")


def test_recovery_after_interrupted_render(series_5min, client):
    """Simulate a crash mid-render: the job is resumed and only the unfinished episode is rendered."""
    pid = series_5min["id"]
    eps = db.query("episodes", "project_id = ?", (pid,), "idx")
    target = eps[4]
    Path(target["video_path"]).unlink()
    db.update("episodes", target["id"], {"status": "rendering"})
    stamps = {e["id"]: e["rendered_at"] for e in eps}
    t = db.now()
    job = db.insert("jobs", {"id": db.new_id(), "type": "build_series", "lane": "render", "project_id": pid,
                             "payload": {}, "status": "running", "progress": 50, "created_at": t, "updated_at": t})
    manager.recover()
    assert db.get("jobs", job["id"])["status"] in ("queued", "running", "done")
    assert db.get("episodes", target["id"])["status"] in ("pending", "rendering", "ready")
    result = wait_job(client, job["id"])
    assert result["status"] == "done", result
    for e in db.query("episodes", "project_id = ?", (pid,), "idx"):
        assert e["status"] == "ready"
        if e["id"] == target["id"]:
            assert e["rendered_at"] > stamps[e["id"]] and Path(e["video_path"]).is_file()
        else:
            assert e["rendered_at"] == stamps[e["id"]]


def test_cancel_job(series_5min, client):
    r = client.post(f"/api/projects/{series_5min['id']}/build", json={"rerender_all": True})
    job = r.json()
    for _ in range(100):
        if client.get(f"/api/jobs/{job['id']}").json()["stage"] in ("Создание части 1 из 5",):
            break
        time.sleep(0.2)
    client.post(f"/api/jobs/{job['id']}/cancel")
    result = wait_job(client, job["id"], timeout=60)
    assert result["status"] == "cancelled"
    assert not db.query("episodes", "project_id = ? AND status = 'rendering'", (series_5min["id"],))
    assert db.get("projects", series_5min["id"])["status"] == "cancelled"
    # retry resumes the series
    retry = client.post(f"/api/jobs/{job['id']}/retry").json()
    assert wait_job(client, retry["id"])["status"] == "done"


def test_project_persisted_in_sqlite(series_5min):
    import sqlite3
    from backend import config
    conn = sqlite3.connect(config.DB_PATH)
    row = conn.execute("SELECT title, status FROM projects WHERE id = ?", (series_5min["id"],)).fetchone()
    n = conn.execute("SELECT COUNT(*) FROM episodes WHERE project_id = ?", (series_5min["id"],)).fetchone()[0]
    conn.close()
    assert row == ("Тест 5 минут", "ready") and n == 5


def test_five_minutes_23_seconds_gives_six_episodes(client):
    main = upload(client, make_source("main_323.mp4", 323), "main")
    gp = upload(client, make_gameplay("gameplay.mp4", duration=20), "gameplay")
    r = client.post("/api/projects", json={"main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
                                           "settings": settings(subtitles_enabled=False, layout="60/40")})
    project = r.json()
    job = wait_job(client, project["job"]["id"])
    assert job["status"] == "done", job
    eps = client.get(f"/api/projects/{project['id']}").json()["episodes"]
    assert len(eps) == 6
    durations = []
    for e in eps:
        v = next(s for s in ffprobe(db.get("episodes", e["id"])["video_path"])["streams"]
                 if s["codec_type"] == "video")
        assert (v["width"], v["height"]) == (1080, 1920)
        durations.append(float(v["duration"]))
    assert durations[:5] == pytest.approx([60.0] * 5, abs=0.01)
    assert durations[5] == pytest.approx(23.0, abs=0.01)
    assert sum(durations) == pytest.approx(323.0, abs=0.05)


def test_vfr_source_is_normalized_and_fully_preserved(client):
    src = MEDIA_DIR / "vfr_main.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=gray:s=160x90:r=20:d=20,geq=lum='16+mod(N\\,50)*4':cb=128:cr=128",
           "-f", "lavfi", "-i", "sine=f=500:d=20",
           "-vf", "setpts='if(lt(N\\,100)\\,N/10\\,10+(N-100)/20)/TB'", "-fps_mode", "vfr",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "15", str(src))
    main = upload(client, src, "main")
    assert main["info"]["is_vfr"]
    gp = upload(client, make_gameplay("gameplay.mp4", duration=20), "gameplay")
    r = client.post("/api/projects", json={"main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
                                           "settings": settings(episode_seconds=10, min_last_seconds=0)})
    project = r.json()
    job = wait_job(client, project["job"]["id"])
    assert job["status"] == "done", job
    p = client.get(f"/api/projects/{project['id']}").json()
    assert p["timeline"]["normalized"] is True
    assert p["timeline"]["duration"] == pytest.approx(main["info"]["video_duration"], abs=0.15)
    assert sum(e["duration"] for e in p["episodes"]) == pytest.approx(p["timeline"]["duration"], abs=1e-6)


def test_webm_ntsc_source_frame_exact(client):
    """WebM (1 ms time base) at 29.97 fps: frame-exact cuts, no extra or missing frames."""
    src = MEDIA_DIR / "ntsc.webm"
    ffmpeg("-f", "lavfi", "-i",
           "color=c=gray:s=160x90:r=30000/1001:d=21,geq=lum='16+mod(N\\,50)*4':cb=128:cr=128",
           "-f", "lavfi", "-i", "sine=f=500:d=21", "-c:v", "libvpx-vp9", "-deadline", "realtime",
           "-lossless", "1", "-c:a", "libopus", str(src))
    main = upload(client, src, "main")
    gp = upload(client, make_gameplay("gameplay.mp4", duration=20), "gameplay")
    r = client.post("/api/projects", json={"main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
                                           "settings": settings(episode_seconds=10, min_last_seconds=0)})
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done", job
    p = client.get(f"/api/projects/{r.json()['id']}").json()
    assert sum(e["end_frame"] - e["start_frame"] for e in p["episodes"]) == main["info"]["nb_frames"]
    for e in p["episodes"]:
        lumas = frame_lumas(db.get("episodes", e["id"])["video_path"])
        expected = [expected_luma(i) for i in range(e["start_frame"], e["end_frame"])]
        assert len(lumas) == len(expected)
        assert all(abs(a - b) <= 4 for a, b in zip(lumas, expected))


def test_default_parts_are_silent_with_subtitles(client):
    """By default no soundtrack/voice is kept: parts carry a silent track and burned-in subtitles only."""
    main = upload(client, make_source("beeps_14.mp4", 14), "main")
    gp = upload(client, make_gameplay("gameplay.mp4", duration=20), "gameplay")
    r = client.post("/api/projects", json={"main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
                                           "settings": {"episode_seconds": 7, "quality": "fast"}})
    assert r.json()["settings"]["main_audio"] is False
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done", job
    p = client.get(f"/api/projects/{r.json()['id']}").json()
    assert len(p["episodes"]) == 2
    for e in p["episodes"]:
        assert e["cues"], "subtitles are still generated from the original speech"
        x = audio_samples(db.get("episodes", e["id"])["video_path"])
        assert len(x) / 48000 == pytest.approx(7.0, abs=0.05)
        assert float(np.abs(x).max()) < 0.001  # the source beeps are not in the output


def test_main_without_audio_gets_silent_track(client):
    main = upload(client, make_source("noaudio.mp4", 12, audio=False), "main")
    gp = upload(client, make_gameplay("gameplay.mp4", duration=20), "gameplay")
    r = client.post("/api/projects", json={"main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
                                           "settings": settings(episode_seconds=10)})
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done", job
    p = client.get(f"/api/projects/{r.json()['id']}").json()
    assert len(p["episodes"]) == 1  # 2 s remainder merged into the single 12 s episode
    assert any("нет звуковой дорожки" in w for w in p["warnings"])
    streams = ffprobe(db.get("episodes", p["episodes"][0]["id"])["video_path"])["streams"]
    assert {s["codec_type"] for s in streams} == {"video", "audio"}


def test_failed_job_reports_russian_error_and_can_retry(client):
    src = MEDIA_DIR / "temp_main.mp4"
    src.write_bytes(make_source("main_short.mp4", 15).read_bytes())
    main = upload(client, src, "main")
    gp = upload(client, make_gameplay("gameplay.mp4", duration=20), "gameplay")
    stored = Path(db.get("media", main["id"])["path"])
    backup = stored.read_bytes()
    stored.unlink()
    r = client.post("/api/projects", json={"main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
                                           "settings": settings(episode_seconds=10)})
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "failed"
    assert "не найден" in job["error"]
    assert client.get(f"/api/projects/{r.json()['id']}").json()["status"] == "error"
    stored.write_bytes(backup)
    db.update("media", main["id"], {"status": "ready"})
    retry = client.post(f"/api/jobs/{job['id']}/retry").json()
    assert wait_job(client, retry["id"])["status"] == "done"
