"""Single-owner device enrollment and session authorization."""
from __future__ import annotations

import argparse
import hashlib
import secrets
import time
from collections import deque
from contextlib import closing
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import config, storage

router = APIRouter(prefix="/api/access", tags=["device access"])
COOKIE = "balaji_session"
SESSION_SECONDS = 30 * 86400
REMOTE_SESSION_SECONDS = 7 * 86400
CODE_SECONDS = 600
rate_lock = Lock()
rate_windows: dict[str, deque[float]] = {}
TOOL_PERMISSIONS = frozenset({"personal", "desktop", "files", "automation", "gmail"})


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def rate_allowed(key: str, limit: int) -> bool:
    now = time.monotonic()
    with rate_lock:
        if key not in rate_windows and len(rate_windows) >= 256:
            for old in list(rate_windows):
                if not rate_windows[old] or now - rate_windows[old][-1] >= 60:
                    del rate_windows[old]
            if len(rate_windows) >= 256:
                return False
        window = rate_windows.setdefault(key, deque())
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


def issue_code(role: str) -> str:
    token = secrets.token_urlsafe(24)
    now = int(time.time())
    with closing(storage.connection()) as db, db:
        db.execute("DELETE FROM access_codes WHERE expires_at<=?", (now,))
        db.execute("INSERT INTO access_codes VALUES(?,?,?)", (digest(token), role, now + CODE_SECONDS))
    return token


def read_session(token: str | None, *, seen: bool = True) -> dict | None:
    if not token or len(token) > 100:
        return None
    now = int(time.time())
    with closing(storage.connection()) as db, db:
        row = db.execute("SELECT id,name,platform,role,status,last_seen,expires_at FROM devices "
                         "WHERE secret_hash=? AND expires_at>? AND status!='revoked'", (digest(token), now)).fetchone()
        if not row:
            return None
        if seen and now - row["last_seen"] >= 10:
            db.execute("UPDATE devices SET last_seen=? WHERE id=? AND status!='revoked'", (now, row["id"]))
        return dict(row)


def allows_tool(actor: str, permission: str) -> bool:
    if permission not in TOOL_PERMISSIONS:
        return False
    if actor == "local":
        return True
    with closing(storage.connection()) as db:
        return db.execute("SELECT 1 FROM devices WHERE id=? AND status='approved' AND expires_at>?",
                          (actor, time.time())).fetchone() is not None


def public_device(row: dict) -> dict:
    return {**row, "online": row["status"] != "revoked" and row["expires_at"] > time.time()
            and time.time() - row["last_seen"] < 35,
            "capabilities": ["chat", "voice", "approvals", *sorted(TOOL_PERMISSIONS)] if row["status"] == "approved" else []}


def audit_device(db, actor: str, action: str, result: str) -> None:
    db.execute("INSERT INTO run_events(actor,action,status,created_at) VALUES(?,?,?,?)",
               (actor, action, result, int(time.time())))


def approved(request: Request) -> dict:
    actor = getattr(request.state, "actor", None)
    if actor is None or actor["status"] != "approved":
        raise HTTPException(401, "Sign in or ask the owner to approve this device.")
    return actor


def owner(request: Request) -> dict:
    actor = approved(request)
    if actor["role"] != "owner":
        raise HTTPException(403, "This operation requires the owner account.")
    return actor


class Enrollment(BaseModel):
    code: str = Field(min_length=20, max_length=80)
    name: str = Field(min_length=1, max_length=60)
    platform: str = Field(default="Browser", max_length=40)


class Decision(BaseModel):
    allow: bool


@router.get("")
def access_status(request: Request):
    actor = request.state.actor
    return {"auth_required": bool(config.REMOTE_HOST), "device": actor}


@router.post("/enroll")
def enroll(body: Enrollment, request: Request, response: Response):
    now = int(time.time())
    lookup = digest(body.code.strip())
    token = secrets.token_urlsafe(32)
    device_id = secrets.token_hex(16)
    with closing(storage.connection()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        code = db.execute("SELECT role FROM access_codes WHERE secret_hash=? AND expires_at>?",
                          (lookup, now)).fetchone()
        if not code or (code["role"] == "owner" and request.state.remote):
            raise HTTPException(400, "This sign-in code is invalid, expired, or requires the laptop browser.")
        role = code["role"]
        status = "approved" if role == "owner" else "pending"
        duration = SESSION_SECONDS if role == "owner" else REMOTE_SESSION_SECONDS
        db.execute("DELETE FROM access_codes WHERE secret_hash=?", (lookup,))
        db.execute("INSERT INTO devices VALUES(?,?,?,?,?,?,?,?)",
                   (device_id, body.name.strip() or "Browser", body.platform.strip(), role,
                    status, digest(token), now + duration, now))
        audit_device(db, device_id, "device_enrolled", status)
    response.set_cookie(COOKIE, token, max_age=duration, httponly=True,
                        secure=request.state.remote, samesite="lax", path="/")
    return {"status": status}


@router.post("/invite")
def invite(request: Request):
    owner(request)
    return {"code": issue_code("device"), "expires_in": CODE_SECONDS}


@router.get("/devices")
def devices(request: Request):
    approved(request)
    with closing(storage.connection()) as db:
        rows = db.execute("SELECT id,name,platform,role,status,last_seen,expires_at FROM devices "
                          "ORDER BY last_seen DESC LIMIT 100")
        return [public_device(dict(row)) for row in rows]


@router.post("/devices/{device_id}")
def decide(device_id: str, body: Decision, request: Request):
    actor = owner(request)
    with closing(storage.connection()) as db, db:
        if body.allow:
            changed = db.execute("UPDATE devices SET status='approved' WHERE id=? AND role='device' "
                                 "AND status='pending' AND expires_at>?", (device_id, time.time()))
        else:
            changed = db.execute("UPDATE devices SET status='revoked' WHERE id=? AND id!=? "
                                 "AND status!='revoked'", (device_id, actor["id"]))
        if not changed.rowcount:
            raise HTTPException(404, "No matching eligible device.")
        audit_device(db, actor["id"], "device_approval" if body.allow else "device_revocation", "succeeded")
    return {"status": "approved" if body.allow else "revoked"}


@router.post("/logout")
def logout(request: Request, response: Response):
    actor = request.state.actor
    if actor and actor["id"] != "local":
        with closing(storage.connection()) as db, db:
            db.execute("UPDATE devices SET status='revoked' WHERE id=?", (actor["id"],))
    response.delete_cookie(COOKIE, path="/", secure=request.state.remote, httponly=True, samesite="lax")
    return {"status": "signed out"}


def main():
    parser = argparse.ArgumentParser(description="Generate a one-use BALAJI laptop-owner sign-in code")
    parser.add_argument("action", choices=["owner"])
    parser.parse_args()
    storage.init_db()
    print(issue_code("owner"))


if __name__ == "__main__":
    main()
