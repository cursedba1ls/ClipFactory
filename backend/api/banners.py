"""Banner pictures: upload (normalised to PNG), list, preview, delete."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from backend import config
from backend.api.common import safe_file_response
from backend.database import db
from backend.errors import UserError
from backend.services import ffmpeg_utils, renderer

router = APIRouter(prefix="/api/banners", tags=["banners"])

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
MAX_BANNER_MB = 20


def _out(path: Path) -> dict:
    return {"id": path.stem, "url": f"/api/banners/{path.stem}.png", "size": path.stat().st_size}


def _convert(src: Path, dest: Path) -> None:
    # One frame, alpha kept, at most 1080 px wide — any picture format FFmpeg reads is accepted.
    ffmpeg_utils.run_ffmpeg(["-i", str(src), "-frames:v", "1",
                             "-vf", "scale='min(1080,iw)':-2:flags=lanczos,format=rgba", str(dest)],
                            error_message="Не удалось прочитать картинку баннера. Используйте PNG или JPG.")


@router.get("")
def list_banners():
    d = config.DIRS["banners"]
    files = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
    return [_out(p) for p in files if p.stem.isalnum()]


@router.post("")
async def upload_banner(request: Request, filename: str = Query(max_length=300)):
    ext = Path(filename or "").suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        raise UserError("Баннер должен быть картинкой: PNG, JPG, WEBP, BMP или GIF.")
    raw = await request.body()
    if not raw:
        raise UserError("Файл пустой.")
    if len(raw) > MAX_BANNER_MB * 1024 * 1024:
        raise UserError(f"Картинка больше {MAX_BANNER_MB} МБ.")
    d = config.DIRS["banners"]
    d.mkdir(parents=True, exist_ok=True)
    bid = db.new_id()
    src = d / f"{bid}.upload{ext}"
    dest = d / f"{bid}.png"
    try:
        src.write_bytes(raw)
        await run_in_threadpool(_convert, src, dest)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        src.unlink(missing_ok=True)
    return _out(dest)


@router.get("/{name}")
def banner_file(name: str):
    stem = name[:-4] if name.endswith(".png") else name
    p = renderer.banner_image_path(stem)
    return safe_file_response(str(p) if p else None, "image/png")


@router.delete("/{banner_id}")
def delete_banner(banner_id: str):
    p = renderer.banner_image_path(banner_id)
    if p:
        p.unlink(missing_ok=True)
    return {"ok": True}
