"""Persistent background job queue.

Jobs live in SQLite, so an application restart never silently loses work: jobs
that were running when the app stopped are put back in the queue and resume
(completed episodes are skipped). Two lanes run in parallel: "io" (downloads,
publishing) and "render" (analysis, transcription, rendering).
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable

from backend.database import db
from backend.errors import Cancelled, UserError
from backend.services import ffmpeg_utils

log = logging.getLogger(__name__)

LANES = ("io", "render")


@dataclass
class JobType:
    name: str
    lane: str
    handler: Callable[["JobContext"], None]
    title: str = ""


_types: dict[str, JobType] = {}


def register(name: str, lane: str, title: str):
    def deco(fn):
        _types[name] = JobType(name, lane, fn, title)
        return fn
    return deco


def job_title(name: str) -> str:
    t = _types.get(name)
    return t.title if t else name


class JobContext:
    def __init__(self, job: dict, cancel_event: threading.Event):
        self.job = job
        self.id = job["id"]
        self.payload = job.get("payload") or {}
        self.cancel_event = cancel_event
        self._last_write = 0.0
        self._state: dict = {}

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def update(self, stage: str | None = None, progress: float | None = None, message: str | None = None,
               eta: float | None = None, force: bool = False) -> None:
        changed_stage = stage is not None and stage != self._state.get("stage")
        if stage is not None:
            self._state["stage"] = stage
        if progress is not None:
            self._state["progress"] = round(max(0.0, min(100.0, progress)), 2)
        if message is not None:
            self._state["message"] = message
        if eta is not None or changed_stage:
            self._state["eta"] = eta
        now = time.monotonic()
        if force or changed_stage or now - self._last_write > 0.5:
            self._last_write = now
            db.update("jobs", self.id, {**self._state, "updated_at": db.now()})


class JobManager:
    def __init__(self) -> None:
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._wake = {lane: threading.Event() for lane in LANES}
        self._running: dict[str, threading.Event] = {}
        self._cancel_requested: set[str] = set()  # cancelled between claim and start
        self._lock = threading.Lock()
        self.shutting_down = False
        self.finish_hooks: list[Callable[[dict, str, str | None], None]] = []

    # --- lifecycle --------------------------------------------------------
    def recover(self) -> int:
        """Re-queue jobs interrupted by a crash or shutdown."""
        rows = db.query("jobs", "status = 'running'")
        for j in rows:
            db.update("jobs", j["id"], {
                "status": "queued", "message": "Возобновлено после перезапуска приложения",
                "updated_at": db.now(),
            })
        # A part interrupted mid-render keeps showing its previous video (if it had one) until it is made again.
        db.connect().execute("UPDATE episodes SET status = CASE WHEN video_path IS NOT NULL THEN 'outdated' "
                             "ELSE 'pending' END WHERE status = 'rendering'")
        db.connect().execute(
            "UPDATE media SET status = 'pending' WHERE status = 'downloading'")
        return len(rows)

    def start(self) -> None:
        self._stop.clear()
        self.shutting_down = False
        n = self.recover()
        if n:
            log.info("Recovered %d interrupted job(s)", n)
        for lane in LANES:
            t = threading.Thread(target=self._loop, args=(lane,), name=f"jobs-{lane}", daemon=True)
            self._threads[lane] = t
            t.start()

    def stop(self, timeout: float = 10) -> None:
        self.shutting_down = True
        self._stop.set()
        with self._lock:
            for ev in self._running.values():
                ev.set()
        ffmpeg_utils.registry.kill_all()
        for ev in self._wake.values():
            ev.set()
        for t in self._threads.values():
            t.join(timeout=timeout)
        self._threads.clear()

    # --- API --------------------------------------------------------------
    def enqueue(self, type_name: str, project_id: str | None = None, media_id: str | None = None,
                episode_id: str | None = None, payload: dict | None = None, dedupe: bool = True) -> dict:
        jt = _types[type_name]
        if dedupe:
            existing = db.query(
                "jobs",
                "type = ? AND status IN ('queued','running') AND IFNULL(project_id,'') = ? "
                "AND IFNULL(media_id,'') = ? AND IFNULL(episode_id,'') = ?",
                (type_name, project_id or "", media_id or "", episode_id or ""),
            )
            if existing:
                if payload and existing[0]["status"] == "queued":
                    merged = {**(existing[0].get("payload") or {}), **payload}
                    db.update("jobs", existing[0]["id"], {"payload": merged})
                    existing[0]["payload"] = merged
                return existing[0]
        t = db.now()
        job = db.insert("jobs", {
            "id": db.new_id(), "type": type_name, "lane": jt.lane, "project_id": project_id,
            "media_id": media_id, "episode_id": episode_id, "payload": payload or {},
            "status": "queued", "stage": "В очереди", "progress": 0, "created_at": t, "updated_at": t,
        })
        self._wake[jt.lane].set()
        return job

    def cancel(self, job_id: str) -> dict | None:
        job = db.get("jobs", job_id)
        if not job:
            return None
        if job["status"] == "queued":
            # Only while still queued: a worker may claim the job at this very moment.
            cur = db.connect().execute(
                "UPDATE jobs SET status = 'cancelled', stage = 'Отменено', finished_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'queued'", (db.now(), db.now(), job_id))
            if cur.rowcount == 0:
                job = db.get("jobs", job_id)
        if job["status"] == "running":
            with self._lock:
                ev = self._running.get(job_id)
                if ev is None:
                    self._cancel_requested.add(job_id)
            if ev:
                ev.set()
            db.update("jobs", job_id, {"message": "Отмена…", "updated_at": db.now()})
        return db.get("jobs", job_id)

    def retry(self, job_id: str) -> dict | None:
        job = db.get("jobs", job_id)
        if not job or job["status"] not in ("failed", "cancelled"):
            return None
        return self.enqueue(job["type"], job.get("project_id"), job.get("media_id"), job.get("episode_id"),
                            job.get("payload") or {})

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._running

    # --- worker -----------------------------------------------------------
    def _claim(self, lane: str) -> dict | None:
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued' AND lane = ? ORDER BY created_at LIMIT 1", (lane,)
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ?, updated_at = ?, attempts = attempts + 1, "
                "error = NULL, error_detail = NULL WHERE id = ?",
                (db.now(), db.now(), row["id"]),
            )
        return db.get("jobs", row["id"])

    def _loop(self, lane: str) -> None:
        while not self._stop.is_set():
            try:
                job = self._claim(lane)
            except Exception:
                log.exception("job claim failed")
                job = None
            if job is None:
                self._wake[lane].wait(timeout=1.0)
                self._wake[lane].clear()
                continue
            self._run(job)

    def _run(self, job: dict) -> None:
        ev = threading.Event()
        with self._lock:
            self._running[job["id"]] = ev
            if job["id"] in self._cancel_requested:
                self._cancel_requested.discard(job["id"])
                ev.set()
        ctx = JobContext(job, ev)
        jt = _types.get(job["type"])
        try:
            if jt is None:
                raise UserError(f"Неизвестный тип задачи: {job['type']}")
            jt.handler(ctx)
            db.update("jobs", job["id"], {
                "status": "done", "stage": "Готово", "progress": 100, "eta": None,
                "finished_at": db.now(), "updated_at": db.now(),
            })
        except Cancelled:
            if self.shutting_down:
                db.update("jobs", job["id"], {"status": "queued", "message": "Прервано при закрытии приложения",
                                              "updated_at": db.now()})
            else:
                db.update("jobs", job["id"], {"status": "cancelled", "stage": "Отменено", "eta": None,
                                              "finished_at": db.now(), "updated_at": db.now()})
                self._on_finish_hook(job, "cancelled", None)
        except UserError as exc:
            log.warning("job %s failed: %s | %s", job["id"], exc.message, (exc.detail or "")[-500:])
            db.update("jobs", job["id"], {
                "status": "failed", "error": exc.message, "error_detail": (exc.detail or "")[-4000:],
                "eta": None, "finished_at": db.now(), "updated_at": db.now(),
            })
            self._on_finish_hook(job, "failed", exc.message)
        except Exception as exc:  # unexpected bug: log full traceback, show a short message
            log.error("job %s crashed:\n%s", job["id"], traceback.format_exc())
            msg = f"Внутренняя ошибка: {exc.__class__.__name__}: {exc}"[:500]
            db.update("jobs", job["id"], {
                "status": "failed", "error": msg, "error_detail": traceback.format_exc()[-4000:],
                "eta": None, "finished_at": db.now(), "updated_at": db.now(),
            })
            self._on_finish_hook(job, "failed", msg)
        finally:
            with self._lock:
                self._running.pop(job["id"], None)

    def _on_finish_hook(self, job: dict, status: str, error: str | None) -> None:
        for hook in self.finish_hooks:
            try:
                hook(job, status, error)
            except Exception:
                log.exception("finish hook failed")


manager = JobManager()
