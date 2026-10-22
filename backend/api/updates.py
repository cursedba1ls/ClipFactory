"""Update routes: status, check, download, install (installed app only)."""
from __future__ import annotations

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from backend.database import db
from backend.services.updater import updater

router = APIRouter(prefix="/api/update", tags=["update"])


def _status() -> dict:
    s = updater.status()
    s["jobs_running"] = db.scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')") or 0
    return s


@router.get("")
def status():
    return _status()


@router.post("/check")
async def check():
    await run_in_threadpool(updater.check)
    return _status()


@router.post("/download")
def download():
    updater.download()
    return _status()


@router.post("/cancel")
def cancel():
    updater.cancel_download()
    return _status()


@router.post("/install")
def install():
    updater.install()
    return _status()


@router.post("/dismiss")
def dismiss():
    updater.dismiss_notice()
    return _status()
