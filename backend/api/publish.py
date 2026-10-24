"""Publication routes: queue, manual workflow data, official TikTok API (OAuth + Content Posting)."""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from backend import config
from backend.api.common import episode_out, get_or_404, job_out
from backend.database import db
from backend.errors import UserError
from backend.jobs.pipeline import tiktok_client
from backend.jobs.queue import manager
from backend.models.schemas import QueueOrderIn, TikTokPublishIn, TikTokSecretIn
from backend.services import publisher, secure_store, settings_store

router = APIRouter(prefix="/api/publish", tags=["publish"])


@router.get("/queue")
def queue():
    eps = db.query("episodes", "deleted = 0 AND (status IN ('ready','outdated') OR publish_status != 'not_published')", (),
                   "CASE WHEN scheduled_at IS NULL THEN 1 ELSE 0 END, scheduled_at, publish_order, idx")
    projects = {p["id"]: p for p in db.query("projects")}
    totals: dict[str, int] = {}
    out = []
    for e in eps:
        p = projects.get(e["project_id"])
        if not p:
            continue
        if p["id"] not in totals:
            totals[p["id"]] = db.scalar("SELECT COUNT(*) FROM episodes WHERE project_id = ?", (p["id"],))
        item = episode_out(e, totals[p["id"]], p["title"])
        out.append(item)
    # Warn when a later part is scheduled before an earlier one of the same series.
    by_project: dict[str, list[dict]] = {}
    for item in out:
        by_project.setdefault(item["project_id"], []).append(item)
    for items in by_project.values():
        scheduled = sorted((i for i in items if i["scheduled_at"]), key=lambda i: i["idx"])
        for a, b in zip(scheduled, scheduled[1:]):
            if b["scheduled_at"] < a["scheduled_at"]:
                b["order_warning"] = f"Часть {b['number']} запланирована раньше части {a['number']}"
    out.sort(key=lambda i: (i["scheduled_at"] is None, i["scheduled_at"] or 0,
                            i["publish_order"] if i["publish_order"] is not None else 10**9, i["idx"]))
    return out


@router.post("/queue/order")
def set_order(body: QueueOrderIn):
    for n, eid in enumerate(body.episode_ids):
        if eid.isalnum():
            db.update("episodes", eid, {"publish_order": n})
    return {"ok": True}


@router.get("/manual-info")
def manual_info():
    return {"upload_url": publisher.MANUAL_UPLOAD_URL}


# --- TikTok official API --------------------------------------------------

def _suggested_redirect() -> str:
    return f"http://127.0.0.1:{config.PORT}/api/publish/tiktok/callback"


@router.get("/tiktok")
def tiktok_status():
    app = settings_store.get_settings()
    client = tiktok_client()
    tokens = client.tokens()
    out = {
        "client_key": app.tiktok_client_key,
        "redirect_uri": app.tiktok_redirect_uri,
        "has_secret": bool(client.client_secret),
        "configured": client.is_configured(),
        "connected": bool(tokens),
        "scope": (tokens or {}).get("scope", ""),
        "suggested_redirect_uri": _suggested_redirect(),
        "manual_upload_url": publisher.MANUAL_UPLOAD_URL,
        "user": None,
        "error": None,
    }
    if tokens and client.is_configured():
        try:
            out["user"] = client.user_info()
        except UserError as exc:
            out["error"] = exc.message
    return out


@router.post("/tiktok/config")
def tiktok_config(body: TikTokSecretIn):
    app = settings_store.get_settings()
    app.tiktok_client_key = body.client_key.strip()
    app.tiktok_redirect_uri = body.redirect_uri.strip()
    settings_store.save_settings(app)
    if body.client_secret.strip():
        secure_store.set_secret("tiktok_client_secret", body.client_secret.strip())
    return tiktok_status()


@router.get("/tiktok/connect")
def tiktok_connect():
    url = tiktok_client().build_auth_url()
    return RedirectResponse(url, status_code=302)


def _page(title: str, text: str) -> HTMLResponse:
    html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>ClipFactory</title>
<style>body{{background:#101116;color:#fff;font-family:Segoe UI,Arial,sans-serif;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}}div{{background:#1B1D24;padding:32px;
border-radius:16px;max-width:480px;border:1px solid #2c2f3a}}a{{color:#F04468}}</style></head>
<body><div><h2>{title}</h2><p>{text}</p><p><a href="/#/publish">Вернуться в ClipFactory</a></p></div></body></html>"""
    return HTMLResponse(html)


@router.get("/tiktok/callback")
def tiktok_callback(code: str | None = Query(default=None, max_length=2000),
                    state: str | None = Query(default=None, max_length=200),
                    error: str | None = Query(default=None, max_length=200),
                    error_description: str | None = Query(default=None, max_length=1000)):
    import html
    if error:
        return _page("Подключение отменено", html.escape(error_description or error))
    if not code or not state:
        return _page("Ошибка подключения", "TikTok не передал код авторизации.")
    try:
        tiktok_client().exchange_code(code, state)
    except UserError as exc:
        return _page("Ошибка подключения", html.escape(exc.message))
    return _page("Аккаунт TikTok подключён", "Теперь части можно выпускать в TikTok прямо из ClipFactory — кнопкой «Выпустить в TikTok» или по расписанию.")


@router.post("/tiktok/disconnect")
def tiktok_disconnect():
    tiktok_client().disconnect()
    return tiktok_status()


@router.get("/tiktok/creator-info")
def tiktok_creator_info():
    client = tiktok_client()
    if not client.tokens():
        raise UserError("Аккаунт TikTok не подключён.")
    info = client.creator_info()
    info["privacy_names"] = {k: publisher.PRIVACY_NAMES.get(k, k) for k in info.get("privacy_level_options") or []}
    return info


@router.post("/episodes/{episode_id}/tiktok")
def publish_episode(episode_id: str, body: TikTokPublishIn):
    e = get_or_404("episodes", episode_id, "Часть")
    if e.get("deleted"):
        raise UserError("Эта часть удалена.")
    if e["publish_status"] == "publishing":
        raise UserError("Эта часть уже публикуется.")
    if e["publish_status"] in ("published", "inbox"):
        raise UserError("Эта часть уже отправлена в TikTok.")
    if e["status"] != "ready":
        raise UserError("Видео части не готово или устарело. Пересоздайте его.")
    client = tiktok_client()
    if not client.is_configured() or not client.tokens():
        raise UserError("Аккаунт TikTok не подключён. Используйте ручную публикацию.")
    info = dict(e.get("publish_info") or {})
    info["privacy_level"] = body.privacy_level
    db.update("episodes", episode_id, {"publish_info": info,
                                       "publish_mode": "api_direct" if body.mode == "direct" else "api_inbox"})
    job = manager.enqueue("tiktok_publish", project_id=e["project_id"], episode_id=episode_id,
                          payload=body.model_dump())
    return job_out(job)


@router.post("/episodes/{episode_id}/refresh")
def refresh_status(episode_id: str):
    e = get_or_404("episodes", episode_id, "Часть")
    info = dict(e.get("publish_info") or {})
    pid = info.get("publish_id")
    if not pid:
        raise UserError("Для этой части нет публикации через API.")
    data = tiktok_client().fetch_status(pid)
    status, msg, _final = publisher.interpret_status(data, info.get("mode", "direct"))
    info.update({"status_raw": data.get("status"), "message": msg, "checked_at": db.now()})
    db.update("episodes", episode_id, {"publish_status": status, "publish_info": info})
    total = db.scalar("SELECT COUNT(*) FROM episodes WHERE project_id = ?", (e["project_id"],))
    return episode_out(db.get("episodes", episode_id), total)
