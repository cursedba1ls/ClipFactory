"""Create the signed update manifest (latest.json) for a built installer.

    CLIPFACTORY_UPDATE_KEY=<base64 private key> python packaging/make_manifest.py dist/ClipFactory-Setup-2.0.1.exe
        [--version 2.0.1] [--url https://.../ClipFactory-Setup-2.0.1.exe] [--out dist/latest.json]
        [--trust-key <extra base64 public key>]   (test builds)

Release notes are taken from the version's section in CHANGELOG.md. The manifest is verified with the app's
own verification code before it is written, so a wrong key fails here and not on users' computers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import update_manifest  # noqa: E402
from backend.update_keys import PUBLIC_KEYS  # noqa: E402
from backend.version import VERSION  # noqa: E402

REPO = "cursedba1ls/ClipFactory"


def release_notes(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## \[?{re.escape(version)}\]?.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("installer")
    ap.add_argument("--version", default=VERSION)
    ap.add_argument("--url", default="")
    ap.add_argument("--out", default=str(ROOT / "dist" / "latest.json"))
    ap.add_argument("--key-file", default="")
    ap.add_argument("--trust-key", default="", help="extra public key accepted for the self-check (test builds)")
    args = ap.parse_args()

    key = Path(args.key_file).read_text().strip() if args.key_file else os.environ.get("CLIPFACTORY_UPDATE_KEY", "")
    if not key.strip():
        raise SystemExit("No signing key: set CLIPFACTORY_UPDATE_KEY (GitHub secret) or pass --key-file.")
    exe = Path(args.installer)
    data = exe.read_bytes()
    url = args.url or f"https://github.com/{REPO}/releases/download/v{args.version}/{exe.name}"
    payload = {
        "app": update_manifest.APP,
        "version": args.version,
        "released_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notes": release_notes(args.version),
        "installer": {"name": exe.name, "url": url, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
    }
    manifest = update_manifest.sign(payload, key)
    keys = list(PUBLIC_KEYS) + ([args.trust_key] if args.trust_key else [])
    update_manifest.verify(manifest, keys)  # self-check with the app's code
    Path(args.out).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Signed manifest for {args.version}: {args.out}")


if __name__ == "__main__":
    main()
