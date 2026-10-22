"""Update manifest format, signing and verification (shared by the app and packaging/release tools).

latest.json:
    {"payload": {"app": "ClipFactory", "version": "2.0.1", "released_at": "...", "notes": "...",
                 "installer": {"name": "...exe", "url": "https://...", "sha256": "<hex>", "size": 123}},
     "signature": "<base64 Ed25519 signature of canonical(payload)>"}

The signature covers the exact canonical JSON bytes of the payload, so nothing in it (version, URL, hash)
can be changed without the private key.
"""
from __future__ import annotations

import base64
import binascii
import json
import re

APP = "ClipFactory"
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(Exception):
    pass


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def parse_version(v: str) -> tuple:
    m = _VERSION.match(str(v or "").strip())
    if not m:
        raise ManifestError(f"Некорректный номер версии: {v!r}")
    major, minor, patch, pre = m.groups()
    # A pre-release sorts before the release with the same numbers: 2.1.0-beta.1 < 2.1.0.
    pre_key = tuple((0, int(x)) if x.isdigit() else (1, x) for x in pre.split(".")) if pre else None
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre_key or ())


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def sign(payload: dict, private_key_b64: str) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64.strip()))
    sig = key.sign(canonical(payload))
    return {"payload": payload, "signature": base64.b64encode(sig).decode("ascii")}


def verify(manifest: dict, public_keys: list[str]) -> dict:
    """Return the payload if the signature is valid for one of the keys and the content is well-formed."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(manifest, dict) or not isinstance(manifest.get("payload"), dict):
        raise ManifestError("Файл обновления повреждён.")
    try:
        sig = base64.b64decode(str(manifest.get("signature") or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError("Подпись обновления повреждена.") from exc
    payload = manifest["payload"]
    data = canonical(payload)
    for k in public_keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(k)).verify(sig, data)
            break
        except (InvalidSignature, ValueError, binascii.Error):
            continue
    else:
        raise ManifestError("Подпись обновления не подтверждена — обновление отклонено.")
    validate_payload(payload)
    return payload


def validate_payload(p: dict) -> None:
    if p.get("app") != APP:
        raise ManifestError("Файл обновления относится к другой программе.")
    parse_version(p.get("version"))
    inst = p.get("installer")
    if not isinstance(inst, dict):
        raise ManifestError("В описании обновления нет установщика.")
    if not isinstance(inst.get("url"), str) or not inst["url"].startswith(("https://", "http://")):
        raise ManifestError("Некорректная ссылка на установщик.")
    if not _SHA256.match(str(inst.get("sha256") or "")):
        raise ManifestError("Некорректная контрольная сумма установщика.")
    size = inst.get("size")
    if not isinstance(size, int) or not (1024 <= size <= 3 * 1024**3):
        raise ManifestError("Некорректный размер установщика.")
    name = str(inst.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}\.exe", name):
        raise ManifestError("Некорректное имя установщика.")
