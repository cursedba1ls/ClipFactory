"""Shared helpers for API routes: serializers, lookups, file responses."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from backend import config
from backend.database import db
from backend.jobs.queue import job_title
from backend.services import downloader, renderer

INFO_FIELDS = ("duration", "video_duration", "width", "height", "fps", "is_vfr", "has_audio", "video_codec",
               "audio_codec", "size", "nb_frames", "container")


def not_found(what: str = "Объект") -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} не найден.")


def get_or_404(table: str, row_id: str, what: str) -> dict:
    if not row_id or not row_id.isalnum() or len(row_id) > 32:
        raise not_found(what)
    row = db.get(table, row_id)
    if not row:
        raise not_found(what)
    return row


def media_out(m: dict | None) -> dict | None:
    if not m:
        return None
    info = m.get("info") or {}
    title = m.get("title") or m.get("original_name") or m.get("source_url") or "Без названия"
    return {
        "id": m["id"],
        "kind": m["kind"],
        "title": title,
        "source_url": m.get("source_url"),
        "original_name": m.get("original_name"),
        "status": m["status"],
        "error": m.get("error"),
        "info": {k: info.get(k) for k in INFO_FIELDS} if info else None,
        "usable_start": m.get("usable_start"),
        "usable_end": m.get("usable_end"),
        "created_at": m["created_at"],
        "file_url": f"/api/media/{m['id']}/file" if m["status"] == "ready" else None,
    }


def job_out(j: dict | None) -> dict | None:
    if not j:
        return None
    return {
        "id": j["id"], "type": j["type"], "title": job_title(j["type"]), "project_id": j.get("project_id"),
        "media_id": j.get("media_id"), "episode_id": j.get("episode_id"), "status": j["status"],
        "stage": j.get("stage"), "progress": j.get("progress") or 0, "eta": j.get("eta"),
        "message": j.get("message"), "error": j.get("error"), "error_detail": j.get("error_detail"),
        "created_at": j["created_at"], "started_at": j.get("started_at"), "finished_at": j.get("finished_at"),
        "updated_at": j.get("updated_at"),
    }


def episode_out(e: dict, total: int, project_title: str | None = None) -> dict:
    v = int((e.get("rendered_at") or 0) * 1000)
    ready = e["status"] in ("ready", "outdated") and e.get("video_path") and Path(e["video_path"]).is_file()
    return {
        "id": e["id"], "project_id": e["project_id"], "idx": e["idx"], "number": e["idx"] + 1, "total": total,
        "start_frame": e["start_frame"], "end_frame": e["end_frame"],
        "start_time": e["start_time"], "end_time": e["end_time"], "duration": e["duration"],
        "status": e["status"], "subtitle_status": e["subtitle_status"], "error": e.get("error"),
        "cues": e.get("cues") or [], "overrides": e.get("overrides") or {}, "gameplay": e.get("gameplay"),
        "verify": e.get("verify"), "approved": bool(e["approved"]),
        "caption": e.get("caption") or "", "hashtags": e.get("hashtags") or "",
        "publish_status": e["publish_status"], "publish_info": e.get("publish_info") or {},
        "publish_mode": e.get("publish_mode") or "manual", "scheduled_at": e.get("scheduled_at"),
        "publish_order": e.get("publish_order"), "rendered_at": e.get("rendered_at"),
        "project_title": project_title,
        "video_url": f"/api/episodes/{e['id']}/video?v={v}" if ready else None,
        "download_url": f"/api/episodes/{e['id']}/download" if ready else None,
        "thumb_url": f"/api/episodes/{e['id']}/thumb?v={v}" if ready and e.get("thumb_path") else None,
        "srt_url": f"/api/episodes/{e['id']}/subtitles.srt",
        "ass_url": f"/api/episodes/{e['id']}/subtitles.ass",
    }


def project_out(p: dict, full: bool = False) -> dict:
    eps = db.query("episodes", "project_id = ?", (p["id"],), "idx")
    total = len(eps)
    counts = {
        "total": total,
        "ready": sum(1 for e in eps if e["status"] == "ready"),
        "approved": sum(1 for e in eps if e["approved"]),
        "published": sum(1 for e in eps if e["publish_status"] in ("published", "manual")),
        "errors": sum(1 for e in eps if e["status"] == "error"),
    }
    jobs = db.query("jobs", "project_id = ? AND type IN ('build_series','render_episodes')", (p["id"],),
                    "created_at DESC LIMIT 1")
    active = db.query("jobs", "project_id = ? AND status IN ('queued','running')", (p["id"],), "created_at")
    thumb = next((f"/api/episodes/{e['id']}/thumb" for e in eps if e.get("thumb_path")), None)
    out = {
        "id": p["id"], "title": p["title"], "status": p["status"], "error": p.get("error"),
        "settings": p.get("settings") or {}, "timeline": p.get("timeline"),
        "warnings": p.get("warnings") or [], "transcript_language": p.get("transcript_language"),
        "main_media_id": p.get("main_media_id"), "gameplay_media_ids": p.get("gameplay_media_ids") or [],
        "created_at": p["created_at"], "updated_at": p["updated_at"], "counts": counts,
        "last_job": job_out(jobs[0]) if jobs else None,
        "active_jobs": [job_out(j) for j in active],
        "thumb_url": thumb,
    }
    if full:
        out["main_media"] = media_out(db.get("media", p["main_media_id"])) if p.get("main_media_id") else None
        out["gameplay_media"] = [media_out(db.get("media", m)) for m in out["gameplay_media_ids"]
                                 if db.get("media", m)]
        out["episodes"] = [episode_out(e, total, p["title"]) for e in eps]
    return out


def safe_file_response(path: str | None, media_type: str, download_name: str | None = None) -> FileResponse:
    """Serve a file only if it lives inside the storage directory (path traversal protection)."""
    if not path:
        raise not_found("Файл")
    p = Path(path)
    if not p.is_file() or not downloader.is_within(p, config.STORAGE):
        raise not_found("Файл")
    if download_name:
        return FileResponse(p, media_type=media_type, filename=download_name)
    return FileResponse(p, media_type=media_type, headers={"Cache-Control": "no-cache"})


def episode_download_name(project_title: str, idx: int, total: int) -> str:
    base = downloader.safe_filename(project_title or "ClipFactory", 80)
    return f"{base} - {renderer.episode_basename(idx, total).replace('Part_', 'Часть ')}.mp4"
