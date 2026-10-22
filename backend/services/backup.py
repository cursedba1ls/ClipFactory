"""Database backups: daily, before schema upgrades and before installing an update.

Backups are consistent SQLite copies (online backup API) in storage/backups; old ones are pruned per reason.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

from backend import config

log = logging.getLogger(__name__)

KEEP = {"daily": 7, "update": 5}
DEFAULT_KEEP = 3
_NAME = re.compile(r"^clipfactory-(?P<reason>[a-z0-9-]+)-(?P<ts>\d{8}-\d{6})\.db$")


def _reason_key(reason: str) -> str:
    return "update" if reason.startswith("update") else reason.split("-")[0]


def backup_database(reason: str, db_path: Path | None = None) -> Path | None:
    """Copy the database; returns the backup path (None if there is nothing to back up or it failed)."""
    from backend.database import db

    src = Path(db_path or db._path())
    if not src.is_file():
        return None
    reason = re.sub(r"[^a-z0-9-]+", "-", reason.lower()).strip("-") or "manual"
    d = config.DIRS["backups"]
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"clipfactory-{reason}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    tmp = dest.with_suffix(".tmp")
    try:
        with sqlite3.connect(str(src), timeout=30) as source, sqlite3.connect(str(tmp)) as target:
            source.backup(target)
        tmp.replace(dest)
    except (sqlite3.Error, OSError) as exc:
        log.warning("database backup (%s) failed: %s", reason, exc)
        tmp.unlink(missing_ok=True)
        return None
    prune(_reason_key(reason))
    log.info("database backup: %s", dest.name)
    return dest


def list_backups() -> list[dict]:
    d = config.DIRS["backups"]
    out = []
    for p in sorted(d.glob("clipfactory-*.db"), reverse=True) if d.is_dir() else []:
        m = _NAME.match(p.name)
        if m:
            out.append({"name": p.name, "reason": m["reason"], "size": p.stat().st_size,
                        "created_at": p.stat().st_mtime})
    return out


def prune(key: str) -> None:
    keep = KEEP.get(key, DEFAULT_KEEP)
    same = [b for b in list_backups() if _reason_key(b["reason"]) == key]
    for b in same[keep:]:
        (config.DIRS["backups"] / b["name"]).unlink(missing_ok=True)


def daily_backup_if_due(max_age_hours: float = 20) -> Path | None:
    latest = next((b for b in list_backups() if b["reason"] == "daily"), None)
    if latest and time.time() - latest["created_at"] < max_age_hours * 3600:
        return None
    return backup_database("daily")
