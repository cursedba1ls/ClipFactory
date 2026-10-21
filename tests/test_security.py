"""Input validation, path traversal, local-only access."""
from pathlib import Path

import pytest

from backend import config
from backend.errors import UserError
from backend.services import downloader
from backend.services.video_composer import EpisodeRenderSpec, build_command
from backend.services.video_splitter import CutWindow


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.com/a", "javascript:alert(1)", "http://localhost/a",
                                 "http://127.0.0.1:8765/api", "http://192.168.1.5/v.mp4", "https://user:pw@x.com/v",
                                 "https://exa mple.com", ""])
def test_bad_urls_rejected(url):
    with pytest.raises(UserError):
        downloader.validate_url(url)


def test_good_url_accepted():
    assert downloader.validate_url(" https://www.youtube.com/watch?v=abc ") == "https://www.youtube.com/watch?v=abc"


@pytest.mark.parametrize("name,expected", [("../../evil.mp4", "_evil.mp4"), ("CON", "video"),
                                           ('a<b>:"c|?*.mp4', "a_b_c_.mp4"), ("", "video")])
def test_safe_filename(name, expected):
    out = downloader.safe_filename(name)
    assert out == expected
    assert "/" not in out and "\\" not in out


def test_is_within():
    assert downloader.is_within(config.STORAGE / "projects" / "x", config.STORAGE)
    assert not downloader.is_within(config.STORAGE / ".." / "etc", config.STORAGE)


def test_upload_rejects_executable(client):
    r = client.post("/api/media/upload?kind=main&filename=virus.exe", content=b"MZ")
    assert r.status_code == 400 and "Неподдерживаемый" in r.json()["detail"]


def test_upload_rejects_fake_video(client):
    r = client.post("/api/media/upload?kind=main&filename=fake.mp4", content=b"not a video at all" * 100)
    assert r.status_code == 400
    assert not list((config.DIRS["downloads"]).glob("*.part"))


@pytest.mark.parametrize("path", ["/api/episodes/..%2F..%2Fetc%2Fpasswd/video", "/api/media/abc$/file",
                                  "/api/projects/" + "a" * 40])
def test_path_traversal_ids(client, path):
    assert client.get(path).status_code == 404


def test_foreign_host_rejected(client):
    r = client.get("/api/dashboard", headers={"Host": "evil.example.com"})
    assert r.status_code == 403


def test_cross_site_post_rejected(client):
    r = client.post("/api/media/url", json={"url": "https://x.com/v", "kind": "main"},
                    headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_ffmpeg_args_are_a_list_without_shell():
    spec = EpisodeRenderSpec(main_path="C:/Videos/my file; rm -rf $(x).mp4", window=CutWindow(0, 0, 10, 0, 10, 10),
                             fps=__import__("fractions").Fraction(30), main_has_audio=True,
                             gameplay_path=None, gameplay_pieces=[])
    cmd = build_command(spec, ["-c:v", "libx264"], ["-c:a", "aac"], "out.mp4")
    assert isinstance(cmd, list)
    assert "C:/Videos/my file; rm -rf $(x).mp4" in cmd  # passed verbatim as one argument


def test_validation_errors_in_russian(client):
    r = client.post("/api/projects", json={"main_media_id": "x", "settings": {"episode_seconds": 1}})
    assert r.status_code == 422 and r.json()["detail"].startswith("Некорректные данные")


def test_divider_line_drawn_on_split():
    from fractions import Fraction
    from backend.services.video_composer import build_filter_and_inputs
    win = CutWindow(0, 0, 10, 0, 10, 10)
    base = dict(main_path="m.mp4", window=win, fps=Fraction(30), main_has_audio=False,
                gameplay_path=None, gameplay_pieces=[], layout="55/45")
    _, graph, _, _ = build_filter_and_inputs(EpisodeRenderSpec(**base, divider=True))
    assert "drawbox=x=0:y=1053:w=iw:h=6:color=black" in graph  # 55/45 split at y=1056
    _, graph, _, _ = build_filter_and_inputs(EpisodeRenderSpec(**base, divider=False))
    assert "drawbox" not in graph


def test_safe_replace_falls_back_when_target_is_locked(tmp_path, monkeypatch):
    import os
    from backend.services import fsutil
    src, dst = tmp_path / "new.mp4", tmp_path / "Part_01.mp4"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    real = os.replace

    def locked(a, b):
        if Path(b) == dst:
            raise PermissionError("in use")
        return real(a, b)
    monkeypatch.setattr(fsutil.os, "replace", locked)
    out = fsutil.safe_replace(src, dst, attempts=2, delay=0)
    assert out != dst and out.read_bytes() == b"new" and dst.read_bytes() == b"old"
