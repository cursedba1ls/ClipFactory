"""System routes: health/dependency check, settings, dashboard statistics."""
from __future__ import annotations

import shutil

from fastapi import APIRouter

from backend import config
from backend.api.common import job_out, project_out
from backend.database import db
from backend.models.schemas import AppSettings
from backend.services import encoders, ffmpeg_utils, settings_store, subtitle_generator

router = APIRouter(prefix="/api", tags=["system"])

WHISPER_MODELS = [
    {"id": "tiny", "name": "tiny — самая быстрая, низкая точность"},
    {"id": "base", "name": "base — быстрая"},
    {"id": "small", "name": "small — баланс (рекомендуется)"},
    {"id": "medium", "name": "medium — точнее, медленнее"},
    {"id": "large-v3", "name": "large-v3 — максимальная точность, очень медленно на CPU"},
]


def _ffmpeg_status() -> dict:
    out = {"ffmpeg": None, "ffprobe": None, "version": None, "libass": False}
    ff = config.find_binary("ffmpeg")
    fp = config.find_binary("ffprobe")
    out["ffmpeg"], out["ffprobe"] = ff, fp
    if ff:
        try:
            proc = ffmpeg_utils.run_simple([ff, "-hide_banner", "-version"], timeout=15)
            out["version"] = proc.stdout.decode("utf-8", "replace").splitlines()[0]
            filters = ffmpeg_utils.run_simple([ff, "-hide_banner", "-filters"], timeout=15)
            out["libass"] = " ass " in filters.stdout.decode("utf-8", "replace")
        except Exception:
            pass
    return out


@router.get("/system")
def system():
    ff = _ffmpeg_status()
    usage = shutil.disk_usage(config.STORAGE)
    models_dir = config.DIRS["models"]
    downloaded = sorted({p.name.replace("models--Systran--faster-whisper-", "")
                         for p in models_dir.glob("models--*")}) if models_dir.is_dir() else []
    return {
        "app": config.APP_NAME, "version": config.APP_VERSION,
        "ffmpeg": ff,
        "whisper_installed": subtitle_generator.whisper_available(),
        "whisper_models_downloaded": downloaded,
        "hardware_encoders": encoders.detect_hardware() if ff["ffmpeg"] else [],
        "storage_path": str(config.STORAGE),
        "disk_free_gb": round(usage.free / 1024**3, 1),
    }


@router.get("/settings")
def get_settings():
    s = settings_store.get_settings()
    return {
        "settings": s.model_dump(),
        "meta": {
            "whisper_models": WHISPER_MODELS,
            "presets": [{"id": k, "name": v, "style": subtitle_generator.PRESETS[k]}
                        for k, v in subtitle_generator.PRESET_NAMES.items()],
            "encoders": encoders.ENCODER_NAMES,
        },
    }


@router.put("/settings")
def put_settings(body: AppSettings):
    return {"settings": settings_store.save_settings(body).model_dump()}


@router.get("/dashboard")
def dashboard():
    c = db.connect()

    def count(sql: str) -> int:
        return c.execute(sql).fetchone()[0]

    stats = {
        "projects": count("SELECT COUNT(*) FROM projects"),
        "episodes": count("SELECT COUNT(*) FROM episodes WHERE status IN ('ready','outdated')"),
        "pending": count("SELECT COUNT(*) FROM episodes WHERE approved = 0 AND publish_status NOT IN "
                         "('published','manual')"),
        "approved": count("SELECT COUNT(*) FROM episodes WHERE approved = 1"),
        "published": count("SELECT COUNT(*) FROM episodes WHERE publish_status IN ('published','manual')"),
    }
    recent = [project_out(p) for p in db.query("projects", "", (), "updated_at DESC LIMIT 6")]
    active = [job_out(j) for j in db.query("jobs", "status IN ('queued','running')", (), "created_at")]
    reminders = []
    for e in db.query("episodes", "scheduled_at IS NOT NULL AND scheduled_at <= ? AND publish_status IN "
                                  "('not_published','scheduled')", (db.now(),), "scheduled_at"):
        p = db.get("projects", e["project_id"])
        reminders.append({"episode_id": e["id"], "project_id": e["project_id"],
                          "project_title": p["title"] if p else "", "number": e["idx"] + 1,
                          "scheduled_at": e["scheduled_at"], "approved": bool(e["approved"])})
    return {"stats": stats, "recent_projects": recent, "active_jobs": active, "reminders": reminders}


@router.get("/backups")
def backups():
    from backend.services import backup

    return {"backups": backup.list_backups(), "path": str(config.DIRS["backups"])}


@router.post("/backups")
def create_backup():
    from backend.errors import UserError
    from backend.services import backup

    p = backup.backup_database("manual")
    if not p:
        raise UserError("Не удалось создать резервную копию. Подробности — в журнале.")
    return {"backups": backup.list_backups(), "created": p.name}
