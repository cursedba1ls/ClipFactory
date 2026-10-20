"""Job routes: list, inspect, cancel, retry."""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.api.common import get_or_404, job_out
from backend.database import db
from backend.errors import UserError
from backend.jobs.queue import manager

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
def list_jobs(active: bool = False, project_id: str | None = Query(default=None, max_length=32),
              limit: int = Query(default=50, ge=1, le=500)):
    where, params = [], []
    if active:
        where.append("status IN ('queued','running')")
    if project_id:
        where.append("project_id = ?")
        params.append(project_id)
    rows = db.query("jobs", " AND ".join(where), tuple(params), f"created_at DESC LIMIT {int(limit)}")
    return [job_out(j) for j in rows]


@router.get("/{job_id}")
def get_job(job_id: str):
    return job_out(get_or_404("jobs", job_id, "Задача"))


@router.post("/{job_id}/cancel")
def cancel(job_id: str):
    get_or_404("jobs", job_id, "Задача")
    return job_out(manager.cancel(job_id))


@router.post("/{job_id}/retry")
def retry(job_id: str):
    get_or_404("jobs", job_id, "Задача")
    job = manager.retry(job_id)
    if not job:
        raise UserError("Повторить можно только завершившуюся с ошибкой или отменённую задачу.")
    if job.get("project_id") and job["type"] in ("build_series", "render_episodes"):
        db.update("projects", job["project_id"], {"status": "processing"})
    return job_out(job)
