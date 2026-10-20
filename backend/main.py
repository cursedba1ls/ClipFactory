"""ClipFactory application entry point (FastAPI + static Russian UI, bound to localhost)."""
from __future__ import annotations

import logging
import logging.handlers
import socket
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import config
from backend.database import db
from backend.errors import UserError

log = logging.getLogger("clipfactory")


class _RedactFilter(logging.Filter):
    """Keep OAuth tokens and secrets out of log files."""

    KEYS = ("access_token", "refresh_token", "client_secret", "Authorization")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(k in msg for k in self.KEYS):
            record.msg = "[сообщение скрыто: содержит секретные данные]"
            record.args = ()
        return True


def setup_logging() -> None:
    config.ensure_dirs()
    root = logging.getLogger()
    if any(getattr(h, "_clipfactory", False) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(config.DIRS["logs"] / "clipfactory.log", maxBytes=5 * 1024 * 1024,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(_RedactFilter())
    fh._clipfactory = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.WARNING)
    sh.addFilter(_RedactFilter())
    sh._clipfactory = True  # type: ignore[attr-defined]
    root.addHandler(sh)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def init_app_state() -> None:
    config.ensure_dirs()
    db.init_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from backend.jobs import pipeline  # noqa: F401  (registers job handlers)
    from backend.jobs.queue import manager
    from backend.jobs.scheduler import scheduler

    setup_logging()
    init_app_state()
    manager.start()
    scheduler.start()
    log.info("ClipFactory started on http://%s:%s", config.HOST, config.PORT)
    try:
        yield
    finally:
        scheduler.stop()
        manager.stop()
        log.info("ClipFactory stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="ClipFactory", version=config.APP_VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Protect against DNS rebinding and cross-site requests from other web pages.
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host and host not in ("127.0.0.1", "localhost", "testserver", "[::1]", config.HOST.lower()):
            return JSONResponse({"detail": "Доступ разрешён только с этого компьютера."}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                o = urlsplit(origin)
                if (o.hostname or "").lower() not in ("127.0.0.1", "localhost", "testserver", config.HOST.lower()):
                    return JSONResponse({"detail": "Запрос с другого сайта отклонён."}, status_code=403)
        return await call_next(request)

    @app.exception_handler(UserError)
    async def user_error(_: Request, exc: UserError):
        return JSONResponse({"detail": exc.message}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        parts = []
        for err in exc.errors()[:5]:
            loc = ".".join(str(x) for x in err.get("loc", []) if x not in ("body", "query"))
            parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
        return JSONResponse({"detail": "Некорректные данные — " + "; ".join(parts)}, status_code=422)

    from backend.api import episodes, jobs, media, projects, publish, system

    for r in (system.router, media.router, projects.router, episodes.router, jobs.router, publish.router):
        app.include_router(r)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(config.FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")
    return app


app = create_app()


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def run() -> None:
    import uvicorn

    setup_logging()
    port = config.PORT
    if not _port_free(config.HOST, port):
        for candidate in range(port + 1, port + 20):
            if _port_free(config.HOST, candidate):
                print(f"Порт {port} занят, использую {candidate}.")
                port = candidate
                break
    config.PORT = port
    url = f"http://127.0.0.1:{port}/"
    print(f"ClipFactory запущен: {url}")
    print("Не закрывайте это окно, пока работаете с программой. Для выхода нажмите Ctrl+C.")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=config.HOST, port=port, log_level="warning", timeout_graceful_shutdown=15)


if __name__ == "__main__":
    run()
