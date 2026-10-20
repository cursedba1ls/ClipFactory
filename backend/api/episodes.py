"""Episode routes: preview files, editing, regeneration, approval, manual publication marks."""
from __future__ import annotations

import json
import random

from fastapi import APIRouter

from backend.api.common import (
    episode_download_name, episode_out, get_or_404, job_out, safe_file_response,
)
from backend.database import db
from backend.errors import UserError
from backend.jobs.queue import manager
from backend.models.schemas import ApproveIn, EpisodePatch, MarkPublishedIn
from backend.services import renderer, subtitle_generator

router = APIRouter(prefix="/api/episodes", tags=["episodes"])


def _total(project_id: str) -> int:
    return db.scalar("SELECT COUNT(*) FROM episodes WHERE project_id = ?", (project_id,)) or 0


def _out(e: dict) -> dict:
    p = db.get("projects", e["project_id"])
    return episode_out(e, _total(e["project_id"]), p["title"] if p else None)


@router.get("/{episode_id}")
def get_episode(episode_id: str):
    e = get_or_404("episodes", episode_id, "Часть")
    out = _out(e)
    p = db.get("projects", e["project_id"])
    out["effective"] = renderer.effective_options(p.get("settings") or {}, e.get("overrides"))
    out["effective"]["subtitle_style"] = subtitle_generator.resolve_style(out["effective"]["subtitle_style"])
    out["effective"]["numbering"] = subtitle_generator.resolve_numbering(out["effective"]["numbering"])
    return out


@router.patch("/{episode_id}")
def patch_episode(episode_id: str, body: EpisodePatch):
    e = get_or_404("episodes", episode_id, "Часть")
    values: dict = {}
    visual = False
    if body.cues is not None:
        cues = subtitle_generator.sanitize_cues([c.model_dump() for c in body.cues], float(e["duration"]))
        values["cues"] = cues
        values["subtitle_status"] = "edited"
        visual = True
    if body.reset_overrides:
        values["overrides"] = {}
        visual = True
    elif body.overrides is not None:
        ov = dict(e.get("overrides") or {})
        new = body.overrides.model_dump(exclude_none=True)
        if "subtitle_style" in new:
            new["subtitle_style"] = {k: v for k, v in new["subtitle_style"].items() if v is not None}
        ov.update(new)
        values["overrides"] = ov
        visual = True
    if body.gameplay is not None:
        g = db.get("media", body.gameplay.media_id) if body.gameplay.media_id.isalnum() else None
        if not g or g["kind"] != "gameplay" or g["status"] != "ready":
            raise UserError("Выбранный геймплей не найден или ещё не загружен.")
        length = float(g.get("usable_end") or g["info"]["video_duration"]) - float(g.get("usable_start") or 0)
        if body.gameplay.random_offset or body.gameplay.offset is None:
            offset = round(random.uniform(0, max(0.0, length - float(e["duration"]))), 3)
        else:
            offset = float(body.gameplay.offset) % max(length, 0.001)
        values["gameplay"] = {"media_id": g["id"], "offset": offset, "manual": True}
        visual = True
    if body.caption is not None:
        values["caption"] = body.caption.strip()
    if body.hashtags is not None:
        values["hashtags"] = body.hashtags.strip()
    if body.clear_schedule:
        values["scheduled_at"] = None
        if e["publish_status"] == "scheduled":
            values["publish_status"] = "not_published"
    elif body.scheduled_at is not None:
        values["scheduled_at"] = float(body.scheduled_at)
        info = dict(e.get("publish_info") or {})
        info.pop("handled_schedule", None)
        info.pop("reminder", None)
        values["publish_info"] = info
        if e["publish_status"] in ("not_published", "failed"):
            values["publish_status"] = "scheduled"
    if body.publish_order is not None:
        values["publish_order"] = body.publish_order
    if body.publish_mode is not None:
        values["publish_mode"] = body.publish_mode
    if visual and e["status"] == "ready":
        values["status"] = "outdated"
    values["updated_at"] = db.now()
    db.update("episodes", episode_id, values)
    job = None
    if body.render:
        job = _enqueue_render(db.get("episodes", episode_id))
    out = _out(db.get("episodes", episode_id))
    out["job"] = job_out(job)
    return out


def _enqueue_render(e: dict) -> dict:
    # Edited content must be reviewed again: re-rendering clears the approval.
    db.update("episodes", e["id"], {"approved": 0})
    active = db.query("jobs", "type = 'render_episodes' AND project_id = ? AND status = 'queued'",
                      (e["project_id"],), "created_at DESC")
    if active:
        ids = list(dict.fromkeys((active[0].get("payload") or {}).get("episode_ids", []) + [e["id"]]))
        # Merge only while the job is still queued; if a worker just claimed it, create a new job.
        cur = db.connect().execute("UPDATE jobs SET payload = ? WHERE id = ? AND status = 'queued'",
                                   (json.dumps({"episode_ids": ids}), active[0]["id"]))
        if cur.rowcount == 1:
            return db.get("jobs", active[0]["id"])
    return manager.enqueue("render_episodes", project_id=e["project_id"],
                           payload={"episode_ids": [e["id"]]}, dedupe=False)


@router.post("/{episode_id}/render")
def render(episode_id: str):
    e = get_or_404("episodes", episode_id, "Часть")
    return job_out(_enqueue_render(e))


@router.post("/{episode_id}/approve")
def approve(episode_id: str, body: ApproveIn):
    e = get_or_404("episodes", episode_id, "Часть")
    if body.approved and e["status"] != "ready":
        raise UserError("Одобрить можно только готовую часть с актуальным видео. Сначала пересоздайте её.")
    db.update("episodes", episode_id, {"approved": 1 if body.approved else 0, "updated_at": db.now()})
    return _out(db.get("episodes", episode_id))


@router.post("/{episode_id}/mark-published")
def mark_published(episode_id: str, body: MarkPublishedIn):
    e = get_or_404("episodes", episode_id, "Часть")
    info = dict(e.get("publish_info") or {})
    if body.published:
        info.update({"manual_url": body.url.strip() or None, "manual_at": db.now(), "reminder": None})
        status = "manual"
    else:
        info.pop("manual_url", None)
        info.pop("manual_at", None)
        status = "scheduled" if e.get("scheduled_at") else "not_published"
    db.update("episodes", episode_id, {"publish_status": status, "publish_info": info, "updated_at": db.now()})
    return _out(db.get("episodes", episode_id))


@router.get("/{episode_id}/video")
def video(episode_id: str, v: str | None = None):
    e = get_or_404("episodes", episode_id, "Часть")
    return safe_file_response(e.get("video_path"), "video/mp4")


@router.get("/{episode_id}/download")
def download(episode_id: str):
    e = get_or_404("episodes", episode_id, "Часть")
    p = db.get("projects", e["project_id"])
    name = episode_download_name(p["title"] if p else "ClipFactory", e["idx"], _total(e["project_id"]))
    return safe_file_response(e.get("video_path"), "video/mp4", name)


@router.get("/{episode_id}/thumb")
def thumb(episode_id: str, v: str | None = None):
    e = get_or_404("episodes", episode_id, "Часть")
    return safe_file_response(e.get("thumb_path"), "image/jpeg")


def _sub_file(e: dict, ext: str):
    p = db.get("projects", e["project_id"])
    total = _total(e["project_id"])
    paths = renderer.episode_paths(e["project_id"], e["idx"], total)
    renderer.write_subtitle_files(p, e, total)
    name = episode_download_name(p["title"], e["idx"], total).replace(".mp4", ext)
    media = "text/plain; charset=utf-8"
    return safe_file_response(str(paths["ass" if ext == ".ass" else "srt"]), media, name)


@router.get("/{episode_id}/subtitles.srt")
def srt(episode_id: str):
    return _sub_file(get_or_404("episodes", episode_id, "Часть"), ".srt")


@router.get("/{episode_id}/subtitles.ass")
def ass(episode_id: str):
    return _sub_file(get_or_404("episodes", episode_id, "Часть"), ".ass")
