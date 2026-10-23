"""End-to-end test of the real Windows installer (run on Windows, see .github/workflows/windows.yml).

    python packaging/tests/e2e_installed.py --setup-a dist/ClipFactory-Setup-2.0.0.exe
        --setup-b dist/ClipFactory-Setup-2.0.1-e2e.exe --key-file build/e2e/key.txt --pub-key <b64>

Covers: silent install (per-user, no admin), shortcuts and uninstall entry, the native window loading the UI
(--selftest), single instance, bundled FFmpeg and speech recognition runtime, a full series render,
persistence of projects/settings/drafts/banners, a rejected tampered update, a real automatic update through
the app's updater (download, verify, silent install, relaunch) with all data preserved, uninstall keeping
user data, and a reinstall that finds the data again.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL = Path(os.environ["LOCALAPPDATA"])
INSTALL = LOCAL / "Programs" / "ClipFactory"
DATA = LOCAL / "ClipFactory"
EXE = INSTALL / "ClipFactory.exe"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F3C5E2A-6B1D-4C7E-9A42-3D5B7E1F0C21}_is1"
START_MENU = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "ClipFactory.lnk"
UPDATE_PORT = 8799
LOGS = ROOT / "build" / "e2e-logs"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_step = [0]


def step(msg: str) -> None:
    _step[0] += 1
    print(f"\n=== [{_step[0]}] {msg}", flush=True)


def check(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}", flush=True)


def port() -> int:
    try:
        return int(json.loads((DATA / "runtime.json").read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError):
        return 8765


def api(path: str, method: str = "GET", body=None, raw: bytes | None = None, timeout: float = 30, p: int | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": "application/octet-stream" if raw is not None else "application/json"}
    req = urllib.request.Request(f"http://127.0.0.1:{p or port()}{path}", data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AssertionError(f"{method} {path} -> {e.code}: {e.read().decode('utf-8', 'replace')}") from e


def try_api(path: str):
    try:
        return api(path, timeout=3)
    except Exception:
        return None


def wait(cond, timeout: float, what: str, interval: float = 1.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = cond()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timeout ({timeout:.0f} s): {what}; last={last!r}")


def run_installer(setup: Path, name: str) -> None:
    log = LOGS / f"{name}.log"
    r = subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/LOG={log}"],
                       timeout=900)
    check(r.returncode == 0, f"{setup.name} exit code 0 (got {r.returncode})")


def reg_version() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            return winreg.QueryValueEx(k, "DisplayVersion")[0]
    except OSError:
        return None


def start_app(extra_env: dict | None = None) -> subprocess.Popen:
    env = dict(os.environ, **(extra_env or {}))
    return subprocess.Popen([str(EXE)], env=env)


def wait_app(version: str, timeout: float = 180) -> dict:
    info = wait(lambda: (lambda i: i if i and i.get("version") == version else None)(try_api("/api/desktop/info")),
                timeout, f"app {version} answering")
    return info


def stop_app(proc: subprocess.Popen | None = None) -> None:
    try:
        api("/api/desktop/quit", "POST", {})
    except Exception as exc:
        print(f"  quit request failed: {exc}")
    if proc is not None:
        proc.wait(timeout=60)
    wait(lambda: try_api("/api/health-check") is None, 60, "app stopped")
    wait(lambda: not app_processes(), 60, "no ClipFactory.exe processes left")


def app_processes() -> list[str]:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ClipFactory.exe", "/NH"], capture_output=True, text=True)
    return [line for line in out.stdout.splitlines() if "ClipFactory.exe" in line]


def wait_job(job_id: str, timeout: float = 900) -> dict:
    return wait(lambda: (lambda j: j if j["status"] in ("done", "failed", "cancelled") else None)(
        api(f"/api/jobs/{job_id}")), timeout, f"job {job_id}", 2)


def make_media(ffmpeg: str, tmp: Path) -> dict:
    tmp.mkdir(parents=True, exist_ok=True)
    main = tmp / "main.mp4"
    game = tmp / "gameplay.mp4"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=24",
                    "-f", "lavfi", "-i", "sine=f=440:d=24", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(main)], check=True)
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "smptehdbars=s=480x360:r=30:d=15",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(game)], check=True)
    srt = tmp / "subs.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nПривет из теста\n\n2\n00:00:12,000 --> 00:00:14,000\n"
                   "Вторая часть\n", encoding="utf-8")
    png = tmp / "banner.png"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0xF04468:s=600x120",
                    "-frames:v", "1", str(png)], check=True)
    return {"main": main, "game": game, "srt": srt, "png": png}


class UpdateServer:
    def __init__(self, directory: Path):
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(directory), **k)  # noqa
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", UPDATE_PORT), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup-a", required=True)
    ap.add_argument("--setup-b", required=True)
    ap.add_argument("--version-a", required=True)
    ap.add_argument("--version-b", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--pub-key", required=True)
    args = ap.parse_args()
    setup_a, setup_b = Path(args.setup_a).resolve(), Path(args.setup_b).resolve()
    LOGS.mkdir(parents=True, exist_ok=True)
    upd = ROOT / "build" / "e2e-update"
    shutil.rmtree(upd, ignore_errors=True)
    upd.mkdir(parents=True)
    update_env = {"CLIPFACTORY_UPDATE_URL": f"http://127.0.0.1:{UPDATE_PORT}/latest.json"}

    step("clean machine")
    check(not EXE.exists() and reg_version() is None, "ClipFactory is not installed yet")
    if DATA.exists():
        shutil.rmtree(DATA)

    step(f"silent install {setup_a.name} (no administrator rights)")
    run_installer(setup_a, "install-a")
    check(EXE.is_file(), f"{EXE} installed")
    check((INSTALL / "ffmpeg" / "ffmpeg.exe").is_file(), "bundled ffmpeg.exe")
    check(reg_version() == args.version_a, f"uninstall entry version {args.version_a}")
    check(START_MENU.is_file(), "Start menu shortcut")

    step("native window loads the interface (--selftest)")
    r = subprocess.run([str(EXE), "--selftest"], timeout=300)
    check(r.returncode == 0, f"selftest exit code 0 (got {r.returncode})")

    step("launch the app")
    proc = start_app(update_env)
    info = wait_app(args.version_a)
    check(info["desktop"] and info["mode"] == "webview", f"runs in its own window (mode={info['mode']})")
    check(info["channel"] == "test", "test build")
    check(Path(info["data_dir"]).resolve() == DATA.resolve(), f"data in {DATA}")
    wait(lambda: api("/api/desktop/info")["ui_ready"], 120, "UI loaded in the window")

    step("a second launch does not start another copy")
    r = subprocess.run([str(EXE)], timeout=60)
    check(r.returncode == 0 and proc.poll() is None, "second instance exited, first keeps running")

    step("bundled tools")
    s = api("/api/system")
    check(s["ffmpeg"]["ffmpeg"] and Path(s["ffmpeg"]["ffmpeg"]).resolve().is_relative_to(INSTALL.resolve()),
          f"uses bundled FFmpeg: {s['ffmpeg']['ffmpeg']}")
    check(s["ffmpeg"]["libass"], "FFmpeg with libass (subtitles)")
    check(s["whisper_installed"], "speech recognition runtime bundled")

    step("make a series (upload, subtitles file, banner, render)")
    media = make_media(str(INSTALL / "ffmpeg" / "ffmpeg.exe"), ROOT / "build" / "e2e-media")
    m = api("/api/media/upload?kind=main&filename=main.mp4", "POST", raw=media["main"].read_bytes(), timeout=120)
    g = api("/api/media/upload?kind=gameplay&filename=gameplay.mp4", "POST", raw=media["game"].read_bytes(),
            timeout=120)
    api(f"/api/media/{m['id']}/subtitles?filename=subs.srt", "POST", raw=media["srt"].read_bytes())
    b = api("/api/banners?filename=banner.png", "POST", raw=media["png"].read_bytes())
    settings = {"episode_seconds": 12, "quality": "fast", "caption_title": "E2E",
                "banner": {"enabled": True, "position": "top", "height": 200, "image_id": b["id"]}}
    p = api("/api/projects", "POST", {"title": "Проект E2E", "main_media_id": m["id"],
                                      "gameplay_media_ids": [g["id"]], "settings": settings})
    job = wait_job(p["job"]["id"])
    check(job["status"] == "done", f"series rendered ({job.get('error')})")
    proj = api(f"/api/projects/{p['id']}")
    check(len(proj["episodes"]) == 2 and all(e["status"] == "ready" for e in proj["episodes"]), "2 parts ready")
    ep_files = sorted((DATA / "projects" / p["id"] / "episodes").glob("Part_*.mp4"))
    check(len(ep_files) == 2 and all(f.stat().st_size > 10000 for f in ep_files), f"MP4 files {ep_files}")

    step("speech recognition with the bundled runtime (tiny model)")
    st = api("/api/settings")["settings"]
    st["whisper_model"] = "tiny"
    st["default_hashtags"] = "#e2e #сохранено"
    api("/api/settings", "PUT", st)
    p2 = api("/api/projects", "POST", {"title": "Речь E2E", "main_media_id": m["id"], "gameplay_media_ids": [g["id"]],
                                       "settings": {"episode_seconds": 24, "quality": "fast"}, "start": False})
    api(f"/api/media/{m['id']}/subtitles", "DELETE")
    j2 = api(f"/api/projects/{p2['id']}/build", "POST", {})
    j2 = wait_job(j2["id"], 1200)
    check(j2["status"] == "done", f"recognition + render finished ({j2.get('error')} {j2.get('error_detail')})")

    step("unsaved editing state is kept on disk")
    ep_id = proj["episodes"][0]["id"]
    api(f"/api/drafts/episode:{ep_id}", "PUT", {"state": {"cues": [{"text": "черновик"}]}, "base": "b"})

    step("a tampered update is rejected")
    payload = upd / "latest.json"
    subprocess.run([sys.executable, str(ROOT / "packaging" / "make_manifest.py"), str(setup_b),
                    "--version", args.version_b, "--url", f"http://127.0.0.1:{UPDATE_PORT}/{setup_b.name}",
                    "--key-file", args.key_file, "--trust-key", args.pub_key, "--out", str(payload)], check=True)
    shutil.copy2(setup_b, upd / setup_b.name)
    good = json.loads(payload.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(good))
    bad["payload"]["installer"]["sha256"] = "0" * 64
    payload.write_text(json.dumps(bad), encoding="utf-8")
    UpdateServer(upd)
    st = api("/api/update/check", "POST", {})
    check(st["state"] == "error" and not st["available"], f"tampered manifest refused: {st['error']}")

    step("automatic update through the app")
    payload.write_text(json.dumps(good), encoding="utf-8")
    st = api("/api/update/check", "POST", {})
    check(st["available"] and st["available"]["version"] == args.version_b, f"update {args.version_b} found")
    api("/api/update/download", "POST", {})
    st = wait(lambda: (lambda s: s if s["state"] in ("ready", "error") else None)(api("/api/update")), 300,
              "download")
    check(st["state"] == "ready", f"installer downloaded and verified ({st['error']})")
    api("/api/update/install", "POST", {})
    proc.wait(timeout=120)
    check(True, "old version closed itself")
    info = wait_app(args.version_b, 600)
    check(info["mode"] == "webview", "new version started in its window")
    check(reg_version() == args.version_b, f"uninstall entry now {args.version_b}")
    st = api("/api/update")
    check(st["just_updated"] and st["just_updated"]["to"] == args.version_b, "update confirmed by the new version")

    step("all data survived the update")
    proj2 = api(f"/api/projects/{p['id']}")
    check(proj2["title"] == "Проект E2E" and len(proj2["episodes"]) == 2, "project and parts")
    check(all(f.is_file() for f in ep_files), "rendered videos")
    check(api("/api/settings")["settings"]["default_hashtags"] == "#e2e #сохранено", "settings")
    check(api(f"/api/drafts/episode:{ep_id}")["draft"]["state"]["cues"][0]["text"] == "черновик", "draft")
    check(any(x["id"] == b["id"] for x in api("/api/banners")), "banner picture")
    check(any(x.name.startswith(f"clipfactory-update-") for x in (DATA / "backups").glob("*.db")),
          "database backed up before the update")
    proc_b = None

    step("installing over a program folder that is still in use")
    stop_app(proc_b)
    busy = subprocess.Popen([str(INSTALL / "ffmpeg" / "ffmpeg.exe"), "-v", "error", "-re", "-f", "lavfi", "-i",
                             "anullsrc", "-t", "300", "-f", "null", "-"], stdin=subprocess.DEVNULL)
    time.sleep(2)
    check(busy.poll() is None, "bundled ffmpeg.exe is running from the program folder")
    run_installer(setup_b, "install-over-busy")
    check(busy.wait(timeout=60) is not None, "the installer stopped it instead of failing on a locked file")
    proc = start_app()
    wait_app(args.version_b)
    check(api(f"/api/projects/{p['id']}")["title"] == "Проект E2E", "app works after reinstalling over it")

    step("quit and uninstall (keeps user data)")
    stop_app(proc)
    uninstaller = INSTALL / "unins000.exe"
    check(uninstaller.is_file(), "uninstaller present")
    subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=300)
    wait(lambda: not EXE.exists(), 180, "program files removed")
    wait(lambda: reg_version() is None, 60, "uninstall entry removed")
    check(not START_MENU.exists(), "Start menu shortcut removed")
    check((DATA / "clipfactory.db").is_file(), "user data kept")

    step("reinstall finds everything again")
    run_installer(setup_b, "reinstall-b")
    proc = start_app()
    wait_app(args.version_b)
    check(api(f"/api/projects/{p['id']}")["title"] == "Проект E2E", "project is back")
    stop_app(proc)
    print("\nALL END-TO-END CHECKS PASSED", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:  # keep the app's own logs with the CI artifact
            for f in (DATA / "logs").glob("*.log"):
                shutil.copy2(f, LOGS / f"app-{f.name}")
        except OSError:
            pass
        log = DATA / "logs" / "clipfactory.log"
        if log.is_file():
            print("\n--- clipfactory.log (tail) ---")
            print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-120:]))
        for f in sorted(LOGS.glob("*.log")):
            print(f"\n--- {f.name} (tail) ---")
            print("\n".join(f.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]))
        raise
