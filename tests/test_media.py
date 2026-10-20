"""Media analysis: duration, streams, VFR detection, corrupted files, black edges."""
import pytest

from backend.errors import MediaError
from backend.services import video_analyzer
from tests.conftest import MEDIA_DIR, ffmpeg, make_gameplay, make_source


def test_duration_and_streams_detected():
    src = make_source("main_300.mp4", 300)
    info = video_analyzer.analyze(src)
    assert info["duration"] == pytest.approx(300, abs=0.1)
    assert info["video_duration"] == pytest.approx(300, abs=0.01)
    assert info["nb_frames"] == 3000
    assert info["fps"] == 10 and not info["is_vfr"]
    assert info["has_audio"] and info["audio_codec"] == "aac"
    assert (info["width"], info["height"]) == (192, 108)


def test_323_seconds():
    info = video_analyzer.analyze(make_source("main_323.mp4", 323))
    assert info["video_duration"] == pytest.approx(323, abs=0.01)
    assert info["nb_frames"] == 3230


def test_missing_audio_detected():
    info = video_analyzer.analyze(make_source("noaudio.mp4", 12, audio=False))
    assert info["has_audio"] is False


def test_vfr_detected():
    out = MEDIA_DIR / "vfr.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=gray:s=160x90:r=20:d=15,geq=lum='16+mod(N\\,50)*4':cb=128:cr=128",
           "-vf", "setpts='if(lt(N\\,100)\\,N/10\\,10+(N-100)/20)/TB'", "-fps_mode", "vfr",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out))
    info = video_analyzer.analyze(out)
    assert info["is_vfr"] is True


def test_corrupted_file_rejected_in_russian():
    bad = MEDIA_DIR / "broken.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"garbage" * 1000)
    with pytest.raises(MediaError) as exc:
        video_analyzer.analyze(bad)
    assert "повреждён" in exc.value.message


def test_text_file_rejected():
    txt = MEDIA_DIR / "notes.mp4"
    txt.write_text("это не видео", encoding="utf-8")
    with pytest.raises(MediaError):
        video_analyzer.analyze(txt)


def test_black_frames_at_gameplay_start_excluded():
    gp = make_gameplay("gp_black.mp4", duration=12, black_head=1.0)
    info = video_analyzer.analyze(gp)
    us, ue = video_analyzer.detect_black_edges(gp, info["video_duration"])
    assert us == pytest.approx(1.0, abs=0.05)
    assert ue == pytest.approx(12.0, abs=0.05)
