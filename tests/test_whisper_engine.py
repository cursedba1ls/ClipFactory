"""Speech recognition through the REAL faster-whisper / CTranslate2 engine.

A tiny random-weight Whisper model is built locally (tests/tiny_whisper.py), so no download is
needed. It cannot understand words, but every integration step runs for real: reading the audio,
features, voice-activity detection, decoding and word timestamps, and the whole "create all
parts" job. This guards against breakages like faster-whisper's decoder failing with newer PyAV:
"TypeError: open() got an unexpected keyword argument 'metadata_errors'".
"""
from pathlib import Path

import pytest

pytest.importorskip("faster_whisper")
pytest.importorskip("ctranslate2")

from backend.services import subtitle_generator as sg  # noqa: E402
from tests import tiny_whisper  # noqa: E402
from tests.conftest import MEDIA_DIR, _TMP, ffmpeg, make_gameplay  # noqa: E402

SPEECH = Path(__file__).parent / "data" / "speech_ru.wav"  # 7.5 s of synthesized Russian speech


@pytest.fixture(scope="module")
def tiny_model() -> str:
    return str(tiny_whisper.build(_TMP / "tiny-whisper"))


@pytest.fixture()
def broken_file_decoder(monkeypatch):
    """faster-whisper's own file decoder fails exactly like it does with PyAV 19 on the user's PC."""
    import faster_whisper.audio
    import faster_whisper.transcribe

    def broken(*args, **kwargs):
        raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")
    monkeypatch.setattr(faster_whisper.audio, "decode_audio", broken)
    monkeypatch.setattr(faster_whisper.transcribe, "decode_audio", broken)


def test_recognition_does_not_depend_on_the_file_decoder(tiny_model, broken_file_decoder):
    result = sg.transcribe(SPEECH, model_size=tiny_model, language=None, device="cpu", duration=7.5)
    assert result["skipped"] == []
    assert result["language"]
    assert result["words"], "the engine produced no words at all"
    for w in result["words"]:
        assert 0 <= w["start"] <= w["end"] <= 7.5 and w["text"]


def test_explicit_language_and_progress(tiny_model):
    progress = []
    result = sg.transcribe(SPEECH, model_size=tiny_model, language="ru", device="cpu",
                           on_progress=progress.append, duration=7.5)
    assert result["skipped"] == []
    assert all(0 <= p <= 1 for p in progress)


def test_create_all_parts_with_the_real_engine(client, tiny_model, broken_file_decoder):
    """The user's scenario: a cartoon with speech → "Создать все части" → parts with subtitles."""
    from tests.test_pipeline import upload, wait_job

    video = MEDIA_DIR / "speech_cartoon.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=7.5", "-i", str(SPEECH),
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video))
    app = client.get("/api/settings").json()["settings"]
    try:
        r = client.put("/api/settings", json={**app, "whisper_model": tiny_model, "whisper_device": "cpu"})
        assert r.status_code == 200, r.text
        main = upload(client, video, "main")
        gp = upload(client, make_gameplay("gp_engine.mp4", duration=10), "gameplay")
        r = client.post("/api/projects", json={
            "title": "Речь", "main_media_id": main["id"], "gameplay_media_ids": [gp["id"]],
            "settings": {"episode_seconds": 5, "min_last_seconds": 1, "quality": "fast"}})
        assert r.status_code == 200, r.text
        job = wait_job(client, r.json()["job"]["id"])
        assert job["status"] == "done", (job.get("error"), job.get("error_detail"))
        project = client.get(f"/api/projects/{r.json()['id']}").json()
        assert project["status"] == "ready"
        assert len(project["episodes"]) == 2
        assert all(e["status"] == "ready" for e in project["episodes"])
        assert not any("распозн" in w.lower() for w in project["warnings"])
        assert sum(len(e["cues"]) for e in project["episodes"]) > 0
    finally:
        client.put("/api/settings", json=app)
