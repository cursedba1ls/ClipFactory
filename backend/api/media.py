"""Media import routes: URL download, streamed file upload, library listing, plan preview."""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from backend import config
from backend.api.common import get_or_404, job_out, media_out, safe_file_response
from backend.database import db
from backend.errors import UserError
from backend.jobs.queue import manager
from backend.models.schemas import MediaPatch, MediaUrlIn
from backend.services import downloader, settings_store, video_analyzer, video_splitter

router = APIRouter(prefix="/api/media", tags=["media"])


@router.get("")
def list_media(kind: str | None = Query(default=None, pattern="^(main|gameplay)$")):
    rows = db.query("media", "kind = ?", (kind,), "created_at DESC") if kind else \
        db.query("media", "", (), "created_at DESC")
    return [media_out(m) for m in rows]


@router.get("/{media_id}")
def get_media(media_id: str):
    m = get_or_404("media", media_id, "Видео")
    jobs = db.query("jobs", "media_id = ? AND status IN ('queued','running')", (media_id,), "created_at DESC")
    out = media_out(m)
    out["job"] = job_out(jobs[0]) if jobs else None
    return out


@router.post("/url")
def add_url(body: MediaUrlIn):
    url = downloader.validate_url(body.url)
    existing = db.query("media", "source_url = ? AND kind = ? AND status IN ('ready','pending','downloading')",
                        (url, body.kind), "created_at DESC")
    for m in existing:
        if m["status"] != "ready" or (m.get("path") and Path(m["path"]).is_file()):
            job = manager.enqueue("download", media_id=m["id"]) if m["status"] != "ready" else None
            out = media_out(m)
            out["job"] = job_out(job)
            out["cached"] = m["status"] == "ready"
            return out
    m = db.insert("media", {
        "id": db.new_id(), "kind": body.kind, "source_url": url, "status": "pending", "created_at": db.now(),
    })
    job = manager.enqueue("download", media_id=m["id"])
    out = media_out(m)
    out["job"] = job_out(job)
    out["cached"] = False
    return out


@router.post("/upload")
async def upload(request: Request, kind: str = Query(pattern="^(main|gameplay)$"),
                 filename: str = Query(max_length=300)):
    """Raw-body streamed upload (no temp copy, size limit enforced while receiving)."""
    ext = downloader.check_upload_name(filename)
    app = settings_store.get_settings()
    limit = app.max_upload_mb * 1024 * 1024
    media_id = db.new_id()
    dest_dir = config.DIRS["downloads"] if kind == "main" else config.DIRS["gameplay"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{media_id}{ext}"
    part = dest.with_suffix(ext + ".part")
    size = 0
    try:
        with part.open("wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise UserError(f"Файл больше допустимого размера ({app.max_upload_mb} МБ). "
                                    "Лимит можно изменить в настройках.")
                await run_in_threadpool(f.write, chunk)
        if size == 0:
            raise UserError("Файл пустой.")
        part.replace(dest)
        info = await run_in_threadpool(video_analyzer.analyze, dest)
    except BaseException:
        downloader.remove_quietly(part)
        downloader.remove_quietly(dest)
        raise
    values = {
        "id": media_id, "kind": kind, "title": Path(downloader.safe_filename(filename)).stem,
        "original_name": downloader.safe_filename(filename), "path": str(dest), "status": "ready",
        "info": info, "created_at": db.now(),
    }
    if kind == "gameplay":
        us, ue = await run_in_threadpool(video_analyzer.detect_black_edges, dest, info["video_duration"])
        values.update({"usable_start": us, "usable_end": ue})
    m = db.insert("media", values)
    return media_out(m)


@router.patch("/{media_id}")
def rename_media(media_id: str, body: MediaPatch):
    get_or_404("media", media_id, "Видео")
    db.update("media", media_id, {"title": body.title.strip() or None})
    return media_out(db.get("media", media_id))


@router.delete("/{media_id}")
def delete_media(media_id: str):
    m = get_or_404("media", media_id, "Видео")
    used_main = db.query("projects", "main_media_id = ?", (media_id,))
    if used_main:
        raise UserError("Это видео используется как основное в проекте «%s». Сначала удалите проект."
                        % used_main[0]["title"])
    for job in db.query("jobs", "media_id = ? AND status IN ('queued','running')", (media_id,)):
        manager.cancel(job["id"])
    for p in db.query("projects", "gameplay_media_ids LIKE ?", (f'%"{media_id}"%',)):
        ids = [i for i in (p.get("gameplay_media_ids") or []) if i != media_id]
        db.update("projects", p["id"], {"gameplay_media_ids": ids})
    if m.get("path") and downloader.is_within(Path(m["path"]), config.STORAGE):
        downloader.remove_quietly(m["path"])
    db.delete("media", media_id)
    return {"ok": True}


@router.get("/{media_id}/file")
def media_file(media_id: str):
    m = get_or_404("media", media_id, "Видео")
    return safe_file_response(m.get("path"), "video/mp4")


@router.get("/{media_id}/plan")
def plan_preview(media_id: str, episode_seconds: float = Query(60, ge=5, le=600),
                 min_last_seconds: float = Query(3, ge=0, le=60)):
    m = get_or_404("media", media_id, "Видео")
    info = m.get("info")
    if m["status"] != "ready" or not info:
        raise UserError("Видео ещё не загружено и не проанализировано.")
    fps = Fraction(int(info["fps_num"]), int(info["fps_den"]))
    total = info.get("nb_frames") or int(round(info["video_duration"] * float(fps)))
    spans = video_splitter.plan_episodes(int(total), fps, episode_seconds, min_last_seconds)
    return {
        "total_frames": total, "fps": float(fps), "duration": float(Fraction(int(total)) / fps),
        "approximate": bool(info.get("is_vfr")),
        "episodes": [{**s.to_dict(), "label": f"{video_splitter.format_timecode(s.start_time)}–"
                                             f"{video_splitter.format_timecode(s.end_time)}"} for s in spans],
    }
