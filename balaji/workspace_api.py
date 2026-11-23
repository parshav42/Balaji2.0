"""Owner workspace controls for memory and allowlisted project files."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import access, activity, desktop, storage, tools

router = APIRouter(prefix="/api")
CATEGORIES = ("saved_notes", "preferences", "projects")


class MemoryEdit(BaseModel):
    fact: str = Field(min_length=1, max_length=500)
    category: str = Field(default="saved_notes", max_length=30)


class CategoryEdit(BaseModel):
    enabled: bool


class ActionRequest(BaseModel):
    arguments: dict
    session_id: str = Field(default="web", pattern=r"^[A-Za-z0-9_-]{1,64}$")


@router.get("/memory")
def memory(q: str = ""):
    with closing(storage.connection()) as db:
        rows = db.execute(
            """SELECT n.id,n.fact,n.created_at,COALESCE(c.category,'saved_notes') AS category
               FROM notes n LEFT JOIN memory_categories c ON c.note_id=n.id
               WHERE instr(lower(n.fact), lower(?)) > 0 OR ?=''
               ORDER BY n.id DESC LIMIT 500""", (q[:150], q[:150]))
        return {"notes": [dict(row) for row in rows],
                "categories": {key: storage.memory_enabled(db, key) for key in CATEGORIES}}


@router.put("/memory/{note_id}")
def edit_memory(note_id: int, body: MemoryEdit, request: Request):
    access.owner(request)
    fact = body.fact.strip()
    if body.category not in CATEGORIES or not fact or tools.SECRET_RE.search(fact):
        raise HTTPException(422, "Choose a category and enter a note without credentials.")
    with closing(storage.connection()) as db, db:
        if not storage.memory_enabled(db, body.category):
            raise HTTPException(409, "Enable that memory category before editing.")
        if not db.execute("UPDATE notes SET fact=? WHERE id=?", (fact, note_id)).rowcount:
            raise HTTPException(404, "Memory not found.")
        db.execute("INSERT INTO memory_categories VALUES(?,?) ON CONFLICT(note_id) DO UPDATE SET category=excluded.category",
                   (note_id, body.category))
    return {"updated": note_id}


@router.delete("/memory/{note_id}")
def delete_memory(note_id: int, request: Request):
    access.owner(request)
    with closing(storage.connection()) as db, db:
        if not db.execute("DELETE FROM notes WHERE id=?", (note_id,)).rowcount:
            raise HTTPException(404, "Memory not found.")
    return {"deleted": note_id}


@router.post("/memory/categories/{category}")
def configure_category(category: str, body: CategoryEdit, request: Request):
    access.owner(request)
    if category not in CATEGORIES:
        raise HTTPException(404, "Unknown note category.")
    with closing(storage.connection()) as db, db:
        db.execute("INSERT INTO memory_settings VALUES(?,?) ON CONFLICT(category) DO UPDATE SET enabled=excluded.enabled",
                   (category, body.enabled))
    return {"enabled": body.enabled}


@router.delete("/history/{session_id}")
def clear_history(session_id: str, request: Request):
    access.owner(request)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
        raise HTTPException(400, "Invalid session ID.")
    storage.clear_history(session_id)
    return {"cleared": session_id}


@router.get("/files/projects")
def projects():
    try:
        return list(desktop.projects())
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "Check the laptop project configuration.") from exc


@router.get("/files")
def list_files(project: str, folder: str = ""):
    try:
        return {"listing": desktop.list_project_files(project, folder)}
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "This allowed project folder could not be opened.") from exc


@router.get("/files/download")
def download(project: str, path: str):
    try:
        parts = desktop.path_parts(path)
        if not parts or Path(path).suffix.lower() not in desktop.READABLE:
            raise ValueError
        with desktop.directory(desktop.project(project), parts[:-1]) as parent:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > desktop.FILE_LIMIT:
                    raise ValueError
                data = source.read(desktop.FILE_LIMIT + 1)
                if len(data) > desktop.FILE_LIMIT:
                    raise ValueError
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "This file is unavailable or outside the permitted file limits.") from exc
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(parts[-1])}"})


@router.post("/actions/{name}")
def request_action(name: str, body: ActionRequest, request: Request):
    actor = access.approved(request)["id"]
    if name not in {"launch_app", "send_gmail_draft"}:
        raise HTTPException(404, "This dashboard action is unavailable.")
    try:
        tools.validate_tool(name, body.arguments)
        preview = tools.approval_preview(name, body.arguments)
        key = request.headers.get("Idempotency-Key", "")
        if key and not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
            raise ValueError("Invalid idempotency key.")
        import json
        run, previous = activity.begin(actor, body.session_id, name + json.dumps(body.arguments, sort_keys=True), key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if previous:
        existing = (previous["result"] or {}).get("pending_approval")
        if not existing:
            raise HTTPException(409, "This dashboard action is already recorded.")
        return existing
    token = uuid4().hex
    storage.create_approval(token, body.session_id, name, body.arguments, run)
    pending = {"id": token, "tool_name": name, "arguments": body.arguments}
    if preview:
        pending["preview"] = preview
    activity.transition(run, "WAITING_FOR_APPROVAL", {"pending_approval": pending})
    return pending
