"""Desktop integration: window state, open folders in Explorer, native "Save as", quit."""
from __future__ import annotations

import shutil
import threading
import urllib.request
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend import build_info, config
from backend.database import db
from backend.errors import UserError
from backend.services import downloader, renderer
from backend.services.shell import open_path, shell

router = APIRouter(prefix="/api/desktop", tags=["desktop"])


@router.get("/info")
def info():
    b = build_info.info()
    return {
        "desktop": shell.desktop, "mode": shell.mode, "version": config.APP_VERSION,
        "channel": b.get("channel"), "commit": b.get("commit"), "built_at": b.get("built_at"),
        "data_dir": str(config.STORAGE), "install_dir": str(config.INSTALL_DIR),
        "can_save_dialog": shell.save_dialog is not None, "ui_ready": shell.ready_at is not None,
    }


@router.post("/ready")
def ready():
    shell.mark_ready()
    return {"ok": True}


@router.post("/show")
def show():
    if shell.show:
        shell.show()
    return {"ok": bool(shell.show)}


@router.post("/quit")
def quit_app():
    if not shell.quit:
        raise UserError("Закрыть программу можно только из окна ClipFactory.")
    threading.Timer(0.3, shell.quit).start()
    return {"ok": True}


class OpenIn(BaseModel):
    target: str = Field(max_length=80, pattern=r"^(data|logs|backups|project:[a-z0-9]+|episode:[a-z0-9]+)$")


@router.post("/open")
def open_folder(body: OpenIn):
    t = body.target
    if t in ("data", "logs", "backups"):
        path = config.STORAGE if t == "data" else config.DIRS[t]
    elif t.startswith("project:"):
        p = db.get("projects", t.split(":", 1)[1])
        if not p:
            raise UserError("Проект не найден.")
        path = renderer.project_dir(p["id"]) / "episodes"
        if not path.is_dir():
            path = renderer.project_dir(p["id"])
    else:
        e = db.get("episodes", t.split(":", 1)[1])
        if not e or not e.get("video_path") or not Path(e["video_path"]).is_file():
            raise UserError("Видео части ещё не создано.")
        path = Path(e["video_path"])
    if not downloader.is_within(path, config.STORAGE):
        raise UserError("Недопустимый путь.")
    open_path(path)
    return {"ok": True}


class SaveIn(BaseModel):
    url: str = Field(max_length=500, pattern=r"^/api/[A-Za-z0-9/_.?=&%-]+$")
    filename: str = Field(max_length=200)


@router.post("/save")
async def save_as(body: SaveIn):
    """Native "Save as" dialog, then the file at `url` (one of our own download routes) is written there."""
    if not shell.save_dialog:
        raise UserError("Сохранение через окно выбора доступно только в программе ClipFactory.")
    name = downloader.safe_filename(body.filename) or "ClipFactory"
    target = await run_in_threadpool(shell.save_dialog, name)
    if not target:
        return {"saved": False}

    def copy() -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        tmp = Path(str(target) + ".part")
        with opener.open(f"http://127.0.0.1:{config.PORT}{body.url}", timeout=600) as resp, tmp.open("wb") as f:
            shutil.copyfileobj(resp, f, 1024 * 1024)
        tmp.replace(target)

    try:
        await run_in_threadpool(copy)
    except OSError as exc:
        raise UserError("Не удалось сохранить файл. Проверьте, что папка доступна для записи.", str(exc)) from exc
    return {"saved": True, "path": str(target)}
