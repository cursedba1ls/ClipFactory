"""Desktop shell without a GUI: window state, server lifecycle, update hand-off, desktop API."""
import json
import threading
import time

import pytest

from backend import config, desktop
from backend.services import updater as updater_mod


def test_window_state_is_sanitised(tmp_path):
    p = tmp_path / "window.json"
    p.write_text(json.dumps({"width": 50, "height": 99999, "x": -5000, "y": 200, "maximized": True}))
    st = desktop.WindowState(p)
    assert st.data["width"] == 1024 and st.data["height"] == 4320
    assert st.data["x"] is None and st.data["y"] == 200 and st.data["maximized"] is True
    p.write_text("not json")
    assert desktop.WindowState(p).data["width"] == 1360


@pytest.fixture
def app_run(monkeypatch, tmp_path):
    """Run the desktop app in --server-only mode on a free port with an isolated database."""
    from backend.database import db

    port = 8890
    monkeypatch.setattr(config, "PORT", port)
    launched = []
    monkeypatch.setattr(updater_mod, "launch_installer", launched.append)
    app = desktop.DesktopApp(["--server-only"])
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", app.run()), daemon=True)
    t.start()
    info = None
    for _ in range(200):
        info = desktop._local_request(config.PORT, "/api/desktop/info")
        if info:
            break
        time.sleep(0.1)
    assert info, "server did not start"
    yield app, t, result, launched
    if t.is_alive():
        app.request_quit()
        t.join(30)
    # The shell's server shares the job queue with the test client: restart it for the following tests.
    from backend.jobs.queue import manager
    from backend.jobs.scheduler import scheduler

    from backend.services.shell import shell

    manager.start()
    scheduler.start()
    shell.__init__()


def test_server_only_lifecycle_and_installer_handoff(app_run, tmp_path):
    app, t, result, launched = app_run
    info = desktop._local_request(config.PORT, "/api/desktop/info")
    assert info["desktop"] is True and info["version"] == config.APP_VERSION
    runtime = json.loads((config.STORAGE / "runtime.json").read_text())
    assert runtime["port"] == config.PORT
    assert desktop._local_request(config.PORT, "/api/desktop/ready", "POST") == {"ok": True}
    assert desktop._local_request(config.PORT, "/api/desktop/info")["ui_ready"] is True
    # The updater hands the verified installer to the shell: the app closes, then the installer starts.
    installer = tmp_path / "ClipFactory-Setup-9.9.9.exe"
    installer.write_bytes(b"MZ")
    app.run_installer_after_exit(installer)
    t.join(30)
    assert not t.is_alive() and result["code"] == 0
    assert launched == [installer]
    assert desktop._local_request(config.PORT, "/api/health-check", timeout=1) is None


def test_quit_via_api(app_run):
    app, t, result, launched = app_run
    assert desktop._local_request(config.PORT, "/api/desktop/quit", "POST") == {"ok": True}
    t.join(30)
    assert not t.is_alive() and launched == []


def test_open_folder_rejects_unknown_targets(client):
    assert client.post("/api/desktop/open", json={"target": "../../windows"}).status_code == 422
    assert client.post("/api/desktop/open", json={"target": "project:nope"}).status_code == 400


def test_save_dialog_only_in_window(client):
    r = client.post("/api/desktop/save", json={"url": "/api/projects/x/export", "filename": "a.zip"})
    assert r.status_code == 400
    r = client.post("/api/desktop/save", json={"url": "http://evil/x", "filename": "a.zip"})
    assert r.status_code == 422
