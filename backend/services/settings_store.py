"""Application settings persisted in the SQLite settings table."""
from __future__ import annotations

import json

from backend.database import db
from backend.models.schemas import AppSettings, ProjectSettings

_KEY = "app"


def get_settings() -> AppSettings:
    raw = db.scalar("SELECT value FROM settings WHERE key = ?", (_KEY,))
    if not raw:
        return AppSettings()
    try:
        return AppSettings.model_validate(json.loads(raw))
    except Exception:
        return AppSettings()


def save_settings(s: AppSettings) -> AppSettings:
    db.connect().execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_KEY, s.model_dump_json()),
    )
    return s


def project_settings(project: dict) -> ProjectSettings:
    try:
        return ProjectSettings.model_validate(project.get("settings") or {})
    except Exception:
        return ProjectSettings()
