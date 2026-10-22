"""Drafts: unsaved editing state saved continuously on disk (survives closing the app or a crash)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Path as PathParam
from pydantic import BaseModel, Field

from backend.database import db
from backend.errors import UserError

router = APIRouter(prefix="/api/drafts", tags=["drafts"])

KEY = PathParam(pattern=r"^[A-Za-z0-9:_-]{1,80}$")
MAX_BYTES = 2 * 1024 * 1024


class DraftIn(BaseModel):
    state: dict
    base: str = Field(default="", max_length=MAX_BYTES)


@router.get("/{key}")
def get_draft(key: str = KEY):
    row = db.connect().execute("SELECT state, base, updated_at FROM drafts WHERE key = ?", (key,)).fetchone()
    if not row:
        return {"draft": None}
    try:
        state = json.loads(row["state"])
    except ValueError:
        return {"draft": None}
    return {"draft": {"state": state, "base": row["base"] or "", "savedAt": row["updated_at"] * 1000}}


@router.put("/{key}")
def put_draft(body: DraftIn, key: str = KEY):
    raw = json.dumps(body.state, ensure_ascii=False)
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise UserError("Черновик слишком большой, чтобы его сохранить.")
    t = db.now()
    db.connect().execute(
        "INSERT INTO drafts(key, state, base, updated_at) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET state = excluded.state, base = excluded.base, updated_at = excluded.updated_at",
        (key, raw, body.base, t))
    return {"ok": True, "savedAt": t * 1000}


@router.delete("/{key}")
def delete_draft(key: str = KEY):
    db.connect().execute("DELETE FROM drafts WHERE key = ?", (key,))
    return {"ok": True}
