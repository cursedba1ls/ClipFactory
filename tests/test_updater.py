"""Update system: signed manifests, verified downloads, install hand-off, confirmation after restart.
Also: lenient settings, database backups and on-disk drafts (data must survive updates)."""
import base64
import hashlib
import http.server
import json
import sqlite3
import threading

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend import config
from backend.errors import UserError
from backend.services import backup, settings_store, update_manifest, updater as updater_mod
from backend.services.update_manifest import ManifestError


def keypair():
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                            serialization.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw,
                                                       serialization.PublicFormat.Raw)).decode()
    return priv, pub


class Server:
    def __init__(self, files: dict):
        self.files = files
        files_ref = files

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = files_ref.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


@pytest.fixture
def env(monkeypatch, tmp_path):
    priv, pub = keypair()
    monkeypatch.setattr(updater_mod, "public_keys", lambda: [pub])
    monkeypatch.setattr(updater_mod, "_insecure_allowed", lambda: True)
    monkeypatch.setattr(updater_mod.build_info, "is_installed_build", lambda: True)
    monkeypatch.setattr(config, "APP_VERSION", "2.0.0")
    monkeypatch.setitem(config.DIRS, "updates", tmp_path / "updates")
    monkeypatch.setitem(config.DIRS, "backups", tmp_path / "backups")
    installer = b"MZ" + b"\x01" * 50000
    srv = Server({})

    def publish(version="2.0.1", body=installer, sha=None, size=None, key=priv, tamper=False):
        payload = {"app": "ClipFactory", "version": version, "released_at": "2026-10-23T00:00:00Z",
                   "notes": "Новое: тест", "installer": {
                       "name": f"ClipFactory-Setup-{version}.exe", "url": srv.url(f"/Setup-{version}.exe"),
                       "sha256": sha or hashlib.sha256(body).hexdigest(), "size": size or len(body)}}
        m = update_manifest.sign(payload, key)
        if tamper:
            m["payload"]["installer"]["url"] = srv.url("/evil.exe")
        srv.files["/latest.json"] = json.dumps(m).encode()
        srv.files[f"/Setup-{version}.exe"] = body

    monkeypatch.setattr(updater_mod, "manifest_url", lambda: srv.url("/latest.json"))
    u = updater_mod.Updater()
    launched = []
    u.installer_launcher = launched.append
    return u, publish, launched, srv


def test_version_order():
    assert update_manifest.is_newer("2.0.1", "2.0.0")
    assert update_manifest.is_newer("2.1.0", "2.1.0-beta.3")
    assert update_manifest.is_newer("2.1.0-beta.10", "2.1.0-beta.9")
    assert not update_manifest.is_newer("2.0.0", "2.0.0")
    assert not update_manifest.is_newer("1.9.9", "2.0.0")
    with pytest.raises(ManifestError):
        update_manifest.parse_version("2.0")


def test_check_download_install_and_confirm(env):
    u, publish, launched, _ = env
    publish()
    st = u.check()
    assert st["state"] == "available" and st["available"]["version"] == "2.0.1"
    u.download(wait=True)
    assert u.state == "ready", u.error
    exe = config.DIRS["updates"] / "ClipFactory-Setup-2.0.1.exe"
    assert exe.is_file()
    u.install()
    assert launched == [exe]
    assert any(b["reason"].startswith("update-2-0-0-to-2-0-1") for b in backup.list_backups())
    pending = json.loads((config.DIRS["updates"] / "state.json").read_text())["pending_install"]
    assert pending["version"] == "2.0.1" and pending["from_version"] == "2.0.0"
    # The installer replaced the program: the new version starts and confirms the update.
    config.APP_VERSION = "2.0.1"
    u2 = updater_mod.Updater()
    u2.startup()
    assert u2.just_updated == {"from": "2.0.0", "to": "2.0.1", "notes": "Новое: тест"}
    assert not exe.exists(), "the used installer is removed"


def test_failed_install_is_reported(env):
    u, publish, launched, _ = env
    publish()
    u.check()
    u.download(wait=True)
    u.install()
    u2 = updater_mod.Updater()  # still 2.0.0: the installer did not finish
    u2.startup()
    assert u2.just_updated is None and "не установилось" in u2.error
    assert u2.state == "ready", "the verified installer stays available for another try"


def test_downloaded_update_survives_a_failed_recheck(env):
    u, publish, launched, srv = env
    publish()
    u.check()
    u.download(wait=True)
    srv.files.pop("/latest.json")  # 404 now
    assert u.check()["state"] == "ready"
    srv.files["/latest.json"] = b"garbage"
    st = u.check()
    assert st["state"] == "ready" and st["error"]
    u.install()
    assert launched


def test_install_while_a_background_check_runs(env, monkeypatch):
    """Regression (found by the Windows end-to-end test): a periodic check that started right after the
    download finished made "Restart and update" fail with "not downloaded yet"."""
    u, publish, launched, srv = env
    publish()
    u.check()
    u.download(wait=True)
    assert u.state == "ready"
    started, release = threading.Event(), threading.Event()
    real_fetch = u._fetch_manifest

    def slow_fetch():
        started.set()
        release.wait(5)
        return real_fetch()

    monkeypatch.setattr(u, "_fetch_manifest", slow_fetch)
    t = threading.Thread(target=u.check)
    t.start()
    assert started.wait(5)
    assert u.status()["state"] == "ready", "a running check must not hide the downloaded update"
    u.install()
    assert launched
    release.set()
    t.join(5)
    assert u.state == "installing", "a check finishing during installation must not change the state"


def test_no_update_when_same_or_older(env):
    u, publish, *_ = env
    publish(version="2.0.0")
    assert u.check()["state"] == "idle"
    publish(version="1.5.0")
    assert u.check()["available"] is None


def test_manifest_not_published_yet(env):
    u, *_ = env
    st = u.check()  # 404
    assert st["state"] == "idle" and st["error"] is None


def test_tampered_manifest_rejected(env):
    u, publish, *_ = env
    publish(tamper=True)
    st = u.check()
    assert st["state"] == "error" and "Подпись" in st["error"]
    assert st["available"] is None


def test_manifest_signed_by_unknown_key_rejected(env):
    u, publish, *_ = env
    other, _ = keypair()
    publish(key=other)
    st = u.check()
    assert st["state"] == "error" and st["available"] is None


def test_corrupted_download_is_deleted_never_installed(env):
    u, publish, launched, srv = env
    publish()
    u.check()
    srv.files["/Setup-2.0.1.exe"] = b"MZ" + b"\x02" * 50000  # same size, different bytes
    u.download(wait=True)
    assert u.state == "error" and "Контрольная сумма" in u.error
    assert not list(config.DIRS["updates"].glob("*.exe")) and not list(config.DIRS["updates"].glob("*.part"))
    with pytest.raises(UserError):
        u.install()
    assert launched == []


def test_installer_modified_after_download_is_refused(env):
    u, publish, launched, _ = env
    publish()
    u.check()
    u.download(wait=True)
    exe = config.DIRS["updates"] / "ClipFactory-Setup-2.0.1.exe"
    exe.write_bytes(b"MZ" + b"\x03" * 50000)
    with pytest.raises(UserError):
        u.install()
    assert launched == [] and not exe.exists()


def test_only_https_github_in_release_builds(monkeypatch):
    monkeypatch.setattr(updater_mod, "_insecure_allowed", lambda: False)
    updater_mod.check_url("https://github.com/cursedba1ls/ClipFactory/releases/latest/download/latest.json")
    for bad in ("http://github.com/x", "https://example.com/latest.json", "http://127.0.0.1:9/latest.json",
                "https://github.com.evil.io/x"):
        with pytest.raises(UserError):
            updater_mod.check_url(bad)


def test_install_requires_installed_build(env, monkeypatch):
    u, publish, launched, _ = env
    publish()
    u.check()
    u.download(wait=True)
    monkeypatch.setattr(updater_mod.build_info, "is_installed_build", lambda: False)
    with pytest.raises(UserError):
        u.install()


def test_update_api_status(client):
    st = client.get("/api/update").json()
    assert st["current_version"] == config.APP_VERSION and "jobs_running" in st
    assert client.post("/api/update/install").status_code == 400


# ---------------------------------------------------------------------------------------------- data safety

def test_settings_survive_an_invalid_value():
    s = settings_store.lenient(settings_store.AppSettings, {
        "encoder": "removed-in-new-version", "max_parallel_renders": 3, "default_hashtags": "#мой",
        "defaults": {"layout": "99/1", "episode_seconds": 45, "banner": {"enabled": True, "height": 5}}})
    assert s.encoder == "auto" and s.max_parallel_renders == 3 and s.default_hashtags == "#мой"
    assert s.defaults.layout == "50/50" and s.defaults.episode_seconds == 45
    assert s.defaults.banner.enabled is True and s.defaults.banner.height == 240


def test_database_backup_is_consistent_copy(tmp_path, monkeypatch):
    from backend.database import db

    monkeypatch.setitem(config.DIRS, "backups", tmp_path / "b")
    db.connect().execute("INSERT OR REPLACE INTO settings(key, value) VALUES('backup-test', '42')")
    p = backup.backup_database("manual")
    assert p and p.is_file()
    with sqlite3.connect(p) as c:
        assert c.execute("SELECT value FROM settings WHERE key = 'backup-test'").fetchone()[0] == "42"
    for _ in range(5):
        backup.backup_database("manual")
    assert len([b for b in backup.list_backups() if b["reason"] == "manual"]) <= backup.DEFAULT_KEEP


def test_schema_upgrade_keeps_data_and_backs_up(tmp_path, monkeypatch):
    from backend.database import db

    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:  # a database from the first release: no new columns, user_version 0
        c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("INSERT INTO settings VALUES ('app', '{\"default_hashtags\": \"#old\"}')")
        c.execute("CREATE TABLE media (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, source_url TEXT, "
                  "original_name TEXT, path TEXT, status TEXT NOT NULL DEFAULT 'pending', info TEXT, error TEXT, "
                  "usable_start REAL, usable_end REAL, created_at REAL NOT NULL)")
        c.execute("INSERT INTO media (id, kind, status, created_at) VALUES ('m1', 'main', 'ready', 1)")
    monkeypatch.setitem(config.DIRS, "backups", tmp_path / "b")
    current = db._path()
    db.set_db_path(old)
    try:
        db.init_db()
        assert db.schema_version() == db.SCHEMA_VERSION
        assert db.get("media", "m1")["status"] == "ready"
        assert "fetch_start" in {r["name"] for r in db.connect().execute("PRAGMA table_info(media)")}
        assert settings_store.get_settings().default_hashtags == "#old"
        assert any(b["reason"].startswith("before-schema") for b in backup.list_backups())
    finally:
        db.set_db_path(current)


def test_drafts_saved_on_disk(client):
    key = "episode:abc123"
    assert client.get(f"/api/drafts/{key}").json() == {"draft": None}
    r = client.put(f"/api/drafts/{key}", json={"state": {"cues": [{"text": "привет"}]}, "base": "x"})
    assert r.status_code == 200
    d = client.get(f"/api/drafts/{key}").json()["draft"]
    assert d["state"]["cues"][0]["text"] == "привет" and d["base"] == "x" and d["savedAt"] > 0
    client.delete(f"/api/drafts/{key}")
    assert client.get(f"/api/drafts/{key}").json() == {"draft": None}
    assert client.get("/api/drafts/bad%20key").status_code == 422
