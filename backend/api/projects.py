"""Project routes: create, list, update, build, export, delete."""
from __future__ import annotations

import random
import shutil
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter

from backend import config
from backend.api.common import episode_download_name, get_or_404, job_out, project_out, safe_file_response
from backend.database import db
from backend.errors import UserError
from backend.jobs.queue import manager
from backend.jobs import pipeline
from backend.models.schemas import BuildRequest, ProjectCreate, ProjectPatch
from backend.services import renderer

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _check_media(main_id: str | None, gameplay_ids: list[str]) -> None:
    if main_id is not None:
        m = db.get("media", main_id) if main_id.isalnum() else None
        if not m or m["kind"] != "main":
            raise UserError("Основное видео не найдено. Загрузите его заново.")
        if m["status"] == "error":
            raise UserError(f"Основное видео не загружено: {m.get('error') or 'ошибка'}")
    for gid in gameplay_ids:
        g = db.get("media", gid) if gid.isalnum() else None
        if not g or g["kind"] != "gameplay":
            raise UserError("Одно из видео геймплея не найдено в библиотеке.")
        if g["status"] == "error":
            raise UserError(f"Геймплей «{g.get('title') or g.get('source_url')}» не загружен: {g.get('error')}")


@router.get("")
def list_projects():
    return [project_out(p) for p in db.query("projects", "", (), "created_at DESC")]


@router.post("")
def create_project(body: ProjectCreate):
    gp_ids = list(dict.fromkeys(body.gameplay_media_ids))
    _check_media(body.main_media_id, gp_ids)
    if not gp_ids:
        raise UserError("Добавьте хотя бы одно видео геймплея.")
    main = db.get("media", body.main_media_id)
    title = body.title.strip() or main.get("title") or main.get("original_name") or \
        time.strftime("Проект от %d.%m.%Y %H:%M")
    settings = body.settings.model_dump()
    if not settings.get("seed"):
        settings["seed"] = random.randint(1, 2**31 - 1)
    t = db.now()
    p = db.insert("projects", {
        "id": db.new_id(), "title": title[:200], "status": "draft", "main_media_id": body.main_media_id,
        "gameplay_media_ids": gp_ids, "settings": settings, "warnings": [], "created_at": t, "updated_at": t,
    })
    job = None
    if body.start:
        job = manager.enqueue("build_series", project_id=p["id"])
        db.update("projects", p["id"], {"status": "processing"})
    out = project_out(db.get("projects", p["id"]), full=True)
    out["job"] = job_out(job)
    return out


@router.get("/{project_id}")
def get_project(project_id: str):
    return project_out(get_or_404("projects", project_id, "Проект"), full=True)


@router.patch("/{project_id}")
def patch_project(project_id: str, body: ProjectPatch):
    p = get_or_404("projects", project_id, "Проект")
    values: dict = {"updated_at": db.now()}
    if body.title is not None:
        values["title"] = body.title.strip() or p["title"]
    if body.gameplay_media_ids is not None:
        ids = list(dict.fromkeys(body.gameplay_media_ids))
        _check_media(None, ids)
        if not ids:
            raise UserError("Добавьте хотя бы одно видео геймплея.")
        if ids != p.get("gameplay_media_ids"):
            values["gameplay_media_ids"] = ids
            for e in db.query("episodes", "project_id = ?", (project_id,)):
                gp = e.get("gameplay") or {}
                if not gp.get("manual"):
                    db.update("episodes", e["id"], {"gameplay": None,
                                                    "status": "outdated" if e["status"] == "ready" else e["status"]})
    if body.settings is not None:
        new = body.settings.model_dump()
        old = p.get("settings") or {}
        new["seed"] = old.get("seed") or new.get("seed") or random.randint(1, 2**31 - 1)
        values["settings"] = new
        visual_keys = ("layout", "divider", "subtitle_style", "numbering", "crop", "quality", "main_audio", "gameplay_audio",
                       "gameplay_volume", "subtitles_enabled")
        if any(new.get(k) != old.get(k) for k in visual_keys):
            db.connect().execute(
                "UPDATE episodes SET status = 'outdated' WHERE project_id = ? AND status = 'ready'", (project_id,))
        if new.get("gameplay_mode") != old.get("gameplay_mode"):
            for e in db.query("episodes", "project_id = ?", (project_id,)):
                if not (e.get("gameplay") or {}).get("manual"):
                    db.update("episodes", e["id"], {"gameplay": None})
    db.update("projects", project_id, values)
    return project_out(db.get("projects", project_id), full=True)


@router.post("/{project_id}/build")
def build(project_id: str, body: BuildRequest | None = None):
    get_or_404("projects", project_id, "Проект")
    body = body or BuildRequest()
    payload = {"retranscribe": body.retranscribe, "rerender_all": body.rerender_all,
               "rebuild_subtitles": body.rerender_all}
    job = manager.enqueue("build_series", project_id=project_id, payload=payload)
    db.update("projects", project_id, {"status": "processing", "updated_at": db.now()})
    return job_out(job)


@router.post("/{project_id}/captions/regenerate")
def regenerate_captions(project_id: str):
    p = get_or_404("projects", project_id, "Проект")
    eps = db.query("episodes", "project_id = ?", (project_id,), "idx")
    transcript = None
    tp = renderer.project_dir(project_id) / "transcript.json"
    if tp.is_file():
        import json
        transcript = json.loads(tp.read_text(encoding="utf-8"))
    pipeline.ensure_captions(p, eps, transcript, force=True)
    return project_out(db.get("projects", project_id), full=True)


@router.post("/{project_id}/approve-all")
def approve_all(project_id: str):
    """Explicit user action: approve every rendered episode of the project."""
    get_or_404("projects", project_id, "Проект")
    db.connect().execute("UPDATE episodes SET approved = 1 WHERE project_id = ? AND status = 'ready'", (project_id,))
    return project_out(db.get("projects", project_id), full=True)


@router.get("/{project_id}/export")
def export_zip(project_id: str):
    p = get_or_404("projects", project_id, "Проект")
    eps = [e for e in db.query("episodes", "project_id = ?", (project_id,), "idx")
           if e["status"] in ("ready", "outdated") and e.get("video_path") and Path(e["video_path"]).is_file()]
    if not eps:
        raise UserError("Нет готовых частей для скачивания.")
    total = db.scalar("SELECT COUNT(*) FROM episodes WHERE project_id = ?", (project_id,))
    out = config.DIRS["exports"] / f"{project_id}.zip"
    tmp = out.with_suffix(".tmp")
    lines = []
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for e in eps:
            name = episode_download_name(p["title"], e["idx"], total)
            z.write(e["video_path"], name)
            paths = renderer.episode_paths(project_id, e["idx"], total)
            if paths["srt"].is_file():
                z.write(paths["srt"], name.replace(".mp4", ".srt"))
            lines.append(f"Часть {e['idx'] + 1}/{total}\n{e.get('caption') or ''}\n{e.get('hashtags') or ''}\n")
        z.writestr("Подписи.txt", "\n".join(lines))
    tmp.replace(out)
    return safe_file_response(str(out), "application/zip", f"{p['title'][:80]}.zip")


@router.delete("/{project_id}")
def delete_project(project_id: str):
    get_or_404("projects", project_id, "Проект")
    for job in db.query("jobs", "project_id = ? AND status IN ('queued','running')", (project_id,)):
        manager.cancel(job["id"])
    deadline = time.time() + 15
    while time.time() < deadline and any(manager.is_running(j["id"]) for j in
                                         db.query("jobs", "project_id = ? AND status = 'running'", (project_id,))):
        time.sleep(0.2)
    shutil.rmtree(renderer.project_dir(project_id), ignore_errors=True)
    (config.DIRS["exports"] / f"{project_id}.zip").unlink(missing_ok=True)
    db.connect().execute("DELETE FROM episodes WHERE project_id = ?", (project_id,))
    db.delete("projects", project_id)
    return {"ok": True}
