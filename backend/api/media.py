"""Media import routes: URL download, streamed file upload, library listing, plan preview."""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from backend import config
from backend.api.common import get_or_404, job_out, media_out, safe_file_response
from backend.database import db
from backend.services import gameplay_manager
from backend.errors import UserError
from backend.jobs.queue import manager
from backend.models.schemas import MediaPatch, MediaUrlIn
from backend.services import (
    downloader, settings_store, smart_crop, subtitle_import, video_analyzer, video_splitter,
)

router = APIRouter(prefix="/api/media", tags=["media"])


@router.get("")
def list_media(kind: str | None = Query(default=None, pattern="^(main|gameplay)$")):
    rows = db.query("media", "kind = ?", (kind,), "created_at DESC") if kind else \
        db.query("media", "", (), "created_at DESC")
    return [media_out(m) for m in rows]


@router.get("/search")
def search(q: str = Query(min_length=1, max_length=200), cc: bool = True,
           limit: int = Query(12, ge=1, le=40), min_seconds: float = Query(0, ge=0),
           max_seconds: float = Query(0, ge=0)):
    """YouTube search for the main video (animation) or gameplay; Creative Commons only by default."""
    return {"results": downloader.search_videos(q, limit, cc, min_seconds, max_seconds), "creative_commons": cc}


@router.get("/{media_id}")
def get_media(media_id: str):
    m = get_or_404("media", media_id, "Видео")
    jobs = db.query("jobs", "media_id = ? AND status IN ('queued','running')", (media_id,), "created_at DESC")
    out = media_out(m)
    out["job"] = job_out(jobs[0]) if jobs else None
    ext = subtitle_import.load_external(media_id)
    out["subtitles_file"] = {"filename": ext["filename"], "cues": len(ext["cues"])} if ext else None
    return out


@router.post("/url")
def add_url(body: MediaUrlIn):
    url = downloader.validate_url(body.url)
    if body.end is not None and body.end - (body.start or 0.0) < 3.0:
        raise UserError("Фрагмент должен быть не короче 3 секунд.")
    fs = round(body.start, 3) if body.start else None
    fe = round(body.end, 3) if body.end is not None else None
    existing = db.query("media", "source_url = ? AND kind = ? AND status IN ('ready','pending','downloading')",
                        (url, body.kind), "created_at DESC")
    existing = [m for m in existing if (m.get("fetch_start"), m.get("fetch_end")) == (fs, fe)]
    for m in existing:
        if m["status"] != "ready" or (m.get("path") and Path(m["path"]).is_file()):
            job = manager.enqueue("download", media_id=m["id"]) if m["status"] != "ready" else None
            out = media_out(m)
            out["job"] = job_out(job)
            out["cached"] = m["status"] == "ready"
            return out
    m = db.insert("media", {
        "id": db.new_id(), "kind": body.kind, "source_url": url, "status": "pending", "created_at": db.now(),
        "fetch_start": fs, "fetch_end": fe,
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
    us, ue = 0.0, None
    if kind == "gameplay":
        us, ue = await run_in_threadpool(video_analyzer.detect_black_edges, dest, info["video_duration"])
        values.update({"usable_start": us, "usable_end": ue})
    info.update(await run_in_threadpool(smart_crop.analyze_media, dest, info, kind, us, ue))
    m = db.insert("media", values)
    return media_out(m)


@router.patch("/{media_id}")
def patch_media(media_id: str, body: MediaPatch):
    m = get_or_404("media", media_id, "Видео")
    values: dict = {}
    if body.title is not None:
        values["title"] = body.title.strip() or None
    trim_changed = False
    if body.reset_trim:
        values.update({"trim_start": None, "trim_end": None})
        trim_changed = True
    elif body.trim_start is not None or body.trim_end is not None:
        if m["kind"] != "gameplay":
            raise UserError("Обрезать можно только видео геймплея.")
        if m["status"] != "ready" or not m.get("info"):
            raise UserError("Видео ещё не загружено и не проанализировано.")
        duration = float(m["info"].get("video_duration") or m["info"].get("duration") or 0)
        start = float(body.trim_start if body.trim_start is not None else (m.get("trim_start") or 0.0))
        end = float(body.trim_end if body.trim_end is not None else (m.get("trim_end") or duration))
        if end > duration + 0.05:
            raise UserError(f"Конец обрезки {fmt_time(end)} дальше длины видео ({fmt_time(duration)}).")
        if end - start < gameplay_manager.MIN_TRIM:
            raise UserError(f"Фрагмент слишком короткий: нужно минимум {int(gameplay_manager.MIN_TRIM)} секунды.")
        values.update({
            "trim_start": round(start, 3) if start > 0.05 else None,
            "trim_end": round(min(end, duration), 3) if end < duration - 0.05 else None,
        })
        trim_changed = True
    if values:
        db.update("media", media_id, values)
    if trim_changed:
        _refocus_gameplay(media_id)
        _outdate_episodes_using(media_id)
    return media_out(db.get("media", media_id))


def fmt_time(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _refocus_gameplay(media_id: str) -> None:
    """Automatic crop focus is measured on the fragment that will actually be used."""
    m = db.get("media", media_id)
    info = dict(m.get("info") or {})
    us, ue = gameplay_manager.gameplay_range(m)
    info["focus"] = smart_crop.focus(m["path"], us, min(60.0, max(0.5, ue - us)),
                                     info["width"], info["height"], 1080, 960, info.get("content_box"), count=24)
    db.update("media", media_id, {"info": info})


def _outdate_episodes_using(media_id: str) -> None:
    """Episodes that used this clip must be re-cut from the new range: re-chosen on the next build."""
    rows = db.query("episodes", "gameplay LIKE ?", (f'%"{media_id}"%',))
    for e in rows:
        gp = e.get("gameplay") or {}
        if gp.get("media_id") != media_id:
            continue
        values = {"updated_at": db.now()}
        if gp.get("manual"):
            values["gameplay"] = {**gp, "offset": 0.0}  # manual choice stays; start over from the new start
        else:
            values["gameplay"] = None
        if e["status"] == "ready":
            values["status"] = "outdated"
        db.update("episodes", e["id"], values)


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


def frame_path(media: dict, t: float, width: int = 640) -> Path:
    """JPEG frame of the picture area (black bars removed) — used by the visual crop editor."""
    info = media.get("info") or {}
    t = max(0.0, min(float(t), float(info.get("video_duration") or 0) - 0.1))
    out = config.DIRS["temp"] / "frames" / f"{media['id']}_{int(t * 10)}_{width}.jpg"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = []
    box = info.get("content_box")
    if box:
        vf.append(f"crop={box['w']}:{box['h']}:{box['x']}:{box['y']}")
    vf.append(f"scale={width}:-2")
    from backend.services import ffmpeg_utils
    ffmpeg_utils.run_ffmpeg(["-ss", ffmpeg_utils.fmt_time(t), "-i", media["path"], "-frames:v", "1",
                             "-vf", ",".join(vf), "-q:v", "3", str(out)],
                            error_message="Не удалось получить кадр из видео.")
    return out


@router.post("/{media_id}/subtitles")
async def upload_subtitles(media_id: str, request: Request, filename: str = Query(max_length=300)):
    """Attach a ready .srt/.vtt file to the main video: used instead of speech recognition."""
    m = get_or_404("media", media_id, "Видео")
    if m["kind"] != "main":
        raise UserError("Субтитры можно прикрепить только к основному видео.")
    if Path(filename).suffix.lower() not in (".srt", ".vtt"):
        raise UserError("Поддерживаются файлы субтитров .srt и .vtt.")
    raw = await request.body()
    data = await run_in_threadpool(subtitle_import.save_external, media_id, filename, raw)
    _invalidate_subtitles(media_id)
    return {"filename": data["filename"], "cues": len(data["cues"])}


@router.delete("/{media_id}/subtitles")
def delete_subtitles(media_id: str):
    get_or_404("media", media_id, "Видео")
    subtitle_import.remove_external(media_id)
    _invalidate_subtitles(media_id)
    return {"ok": True}


def _invalidate_subtitles(media_id: str) -> None:
    """Projects using this video regenerate their (non-edited) subtitles on the next build."""
    for p in db.query("projects", "main_media_id = ?", (media_id,)):
        db.connect().execute(
            "UPDATE episodes SET subtitle_status = 'none', status = CASE WHEN status = 'ready' THEN 'outdated' "
            "ELSE status END WHERE project_id = ? AND subtitle_status != 'edited'", (p["id"],))


@router.get("/{media_id}/frame")
def media_frame(media_id: str, t: float = Query(0, ge=0), w: int = Query(640, ge=64, le=1280)):
    m = get_or_404("media", media_id, "Видео")
    if m["status"] != "ready":
        raise UserError("Видео ещё не загружено.")
    return safe_file_response(str(frame_path(m, t, w)), "image/jpeg")


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
