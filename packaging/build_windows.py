"""Build the Windows installer: dist/ClipFactory-Setup-<version>.exe (run on Windows, see RELEASING.md).

    python packaging/build_windows.py                       release build of backend/version.py
    python packaging/build_windows.py --channel test --extra-public-key <b64> --allow-insecure-updates
    python packaging/build_windows.py --reuse-app --version 2.0.1-test   (re-wrap the last build, other version)

Steps: build.json + exe version info → bundled FFmpeg (pinned, hash-checked) → PyInstaller (one folder)
→ Inno Setup (per-user installer, no admin rights) → SHA-256 of the installer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "clipfactory"
APP_DIR = DIST / "ClipFactory"
CACHE = ROOT / "build" / "cache"

sys.path.insert(0, str(ROOT))
from backend.version import VERSION  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def numeric_version(v: str) -> str:
    nums = [int(x) for x in re.match(r"^(\d+)\.(\d+)\.(\d+)", v).groups()]
    return ".".join(str(x) for x in nums + [0])


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def write_build_info(target_dir: Path, version: str, args) -> dict:
    info = {
        "version": version,
        "channel": args.channel,
        "commit": os.environ.get("GITHUB_SHA", "")[:7] or git_commit(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "extra_public_keys": [args.extra_public_key] if args.extra_public_key else [],
        "allow_insecure_updates": bool(args.allow_insecure_updates),
    }
    if args.update_url:
        info["update_url"] = args.update_url
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "build.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    return info


def write_version_resource(version: str) -> None:
    n = numeric_version(version).replace(".", ", ")
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({n}), prodvers=({n}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'ClipFactory'),
      StringStruct('FileDescription', 'ClipFactory — серии вертикальных видео'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'ClipFactory'),
      StringStruct('OriginalFilename', 'ClipFactory.exe'),
      StringStruct('ProductName', 'ClipFactory'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1049, 1200])])
  ]
)
"""
    (BUILD / "version_info.txt").write_text(text, encoding="utf-8")


def fetch_ffmpeg() -> Path:
    """Download (cached) and verify the pinned FFmpeg build; returns the folder with ffmpeg.exe/ffprobe.exe."""
    spec = json.loads((PKG / "ffmpeg.json").read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / f"ffmpeg-{spec['version']}.zip"
    if not archive.is_file():
        last = None
        for url in spec["urls"]:
            try:
                print(f"Downloading FFmpeg: {url}", flush=True)
                tmp = archive.with_suffix(".part")
                with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as f:
                    shutil.copyfileobj(resp, f, 1 << 20)
                tmp.replace(archive)
                break
            except OSError as exc:
                last = exc
                print(f"  failed: {exc}", flush=True)
        else:
            raise SystemExit(f"Could not download FFmpeg: {last}")
    digest = sha256(archive)
    print(f"FFmpeg archive sha256: {digest}", flush=True)
    if spec.get("sha256"):
        if digest != spec["sha256"]:
            archive.unlink()
            raise SystemExit("FFmpeg archive hash mismatch — refusing to bundle it.")
    else:
        print("WARNING: packaging/ffmpeg.json has no sha256 pinned yet; pin the value above.", flush=True)
    out = CACHE / f"ffmpeg-{spec['version']}"
    if not (out / "ffmpeg.exe").is_file():
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1]
                if base in ("ffmpeg.exe", "ffprobe.exe") or (base == "LICENSE" and name.count("/") == 1):
                    with z.open(name) as src, (out / base).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
    for exe in ("ffmpeg.exe", "ffprobe.exe"):
        if not (out / exe).is_file():
            raise SystemExit(f"{exe} not found in the FFmpeg archive")
    return out


def run_pyinstaller() -> None:
    env = dict(os.environ, CLIPFACTORY_BUILD_DIR=str(BUILD))
    shutil.rmtree(APP_DIR, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller"),
                    str(PKG / "clipfactory.spec")], cwd=ROOT, env=env, check=True)


VC_RUNTIME = ["msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll",
              "vcomp140.dll", "concrt140.dll"]


def bundle_vc_runtime(app_dir: Path) -> None:
    """App-local Visual C++ runtime (allowed by Microsoft): speech recognition and other native modules
    then work on PCs without the VC++ Redistributable installed."""
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    target = app_dir / "_internal"
    for name in VC_RUNTIME:
        src = system32 / name
        if src.is_file() and not (target / name).exists():
            shutil.copy2(src, target / name)
            print(f"  bundled {name}", flush=True)


def find_iscc() -> str:
    for c in [os.environ.get("ISCC"), shutil.which("ISCC"), shutil.which("iscc"),
              r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe",
              str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe")]:
        if c and Path(c).is_file():
            return c
    raise SystemExit("Inno Setup 6 (ISCC.exe) not found. Install it: choco install innosetup")


def build_installer(app_dir: Path, version: str) -> Path:
    out = DIST
    subprocess.run([find_iscc(), f"/DAppVersion={version}", f"/DAppVersionNumeric={numeric_version(version)}",
                    f"/DSourceDir={app_dir}", f"/DOutputDir={out}", f"/DIconFile={PKG / 'clipfactory.ico'}",
                    str(PKG / "installer.iss")], check=True)
    exe = out / f"ClipFactory-Setup-{version}.exe"
    if not exe.is_file():
        raise SystemExit(f"Installer not produced: {exe}")
    return exe


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=VERSION, help="override (test builds)")
    ap.add_argument("--channel", default="stable", choices=["stable", "beta", "test"])
    ap.add_argument("--extra-public-key", default="", help="test builds: additional trusted update key")
    ap.add_argument("--allow-insecure-updates", action="store_true", help="test builds: http://127.0.0.1 updates")
    ap.add_argument("--update-url", default="", help="override the update manifest URL")
    ap.add_argument("--reuse-app", action="store_true", help="re-wrap dist/ClipFactory with a new build.json")
    ap.add_argument("--app-dir-name", default="ClipFactory", help="folder under dist/ to package (with --reuse-app)")
    ap.add_argument("--no-installer", action="store_true")
    args = ap.parse_args()
    if args.channel == "stable" and (args.extra_public_key or args.allow_insecure_updates):
        raise SystemExit("Test-only update options are not allowed in stable builds.")

    BUILD.mkdir(parents=True, exist_ok=True)
    if args.reuse_app:
        src = DIST / "ClipFactory"
        app_dir = DIST / args.app_dir_name
        if app_dir != src:
            shutil.rmtree(app_dir, ignore_errors=True)
            shutil.copytree(src, app_dir)
        info = write_build_info(app_dir / "_internal", args.version, args)
    else:
        info = write_build_info(BUILD, args.version, args)
        write_version_resource(args.version)
        ff = fetch_ffmpeg()
        run_pyinstaller()
        app_dir = APP_DIR
        bundle_vc_runtime(app_dir)
        (app_dir / "ffmpeg").mkdir(exist_ok=True)
        for f in ff.iterdir():
            shutil.copy2(f, app_dir / "ffmpeg" / f.name)
    print(f"App folder: {app_dir}  build.json: {json.dumps(info, ensure_ascii=False)}", flush=True)
    if args.no_installer:
        return
    exe = build_installer(app_dir, args.version)
    digest = sha256(exe)
    (exe.with_suffix(".exe.sha256")).write_text(f"{digest}  {exe.name}\n", encoding="ascii")
    print(f"Installer: {exe} ({exe.stat().st_size / 1048576:.1f} MB) sha256={digest}", flush=True)


if __name__ == "__main__":
    main()
