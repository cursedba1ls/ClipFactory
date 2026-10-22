"""Application settings persisted in the SQLite settings table."""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ValidationError

from backend.database import db
from backend.models.schemas import AppSettings, ProjectSettings

log = logging.getLogger(__name__)

_KEY = "app"


def get_settings() -> AppSettings:
    raw = db.scalar("SELECT value FROM settings WHERE key = ?", (_KEY,))
    if not raw:
        return AppSettings()
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("settings are not valid JSON; using defaults")
        return AppSettings()
    return lenient(AppSettings, data)


def lenient(model: type[BaseModel], data) -> BaseModel:
    """Validate, keeping every valid value: one bad or outdated field (e.g. after an update changed
    the allowed values) falls back to its default instead of resetting all settings."""
    if not isinstance(data, dict):
        return model()
    try:
        return model.model_validate(data)
    except ValidationError:
        pass
    clean = {}
    for name, field in model.model_fields.items():
        if name not in data:
            continue
        value = data[name]
        sub = field.annotation
        if isinstance(sub, type) and issubclass(sub, BaseModel) and isinstance(value, dict):
            clean[name] = lenient(sub, value).model_dump()
            continue
        try:
            model.model_validate({**clean, name: value})
            clean[name] = value
        except ValidationError:
            log.warning("setting %s.%s=%r is not valid any more; using the default", model.__name__, name, value)
    return model.model_validate(clean)


def save_settings(s: AppSettings) -> AppSettings:
    db.connect().execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_KEY, s.model_dump_json()),
    )
    return s


def project_settings(project: dict) -> ProjectSettings:
    return lenient(ProjectSettings, project.get("settings") or {})
