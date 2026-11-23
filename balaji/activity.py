"""Durable run progress, bounded audit metadata and idempotent chat submissions."""
from __future__ import annotations

import json
import time
from contextlib import closing
from uuid import uuid4

from . import access, storage


def begin(actor: str, session: str, question: str, key: str) -> tuple[str, dict | None]:
    signature = access.digest(json.dumps([session, question], ensure_ascii=False))
    run_id = access.digest(f"{actor}\0{key}") if key else uuid4().hex
    now = int(time.time())
    with closing(storage.connection()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT state,request_hash,result FROM runs WHERE id=?", (run_id,)).fetchone()
        if previous:
            if previous["request_hash"] != signature:
                raise ValueError("This request key was already used for another message.")
            result = json.loads(previous["result"]) if previous["result"] else None
            return run_id, {"state": previous["state"], "result": result}
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,NULL,?,?)",
                   (run_id, actor, session, signature, "RECEIVED", now, now))
    return run_id, None


def transition(run_id: str | None, state: str, result: dict | None = None) -> None:
    if not run_id:
        return
    with closing(storage.connection()) as db, db:
        db.execute("UPDATE runs SET state=?,updated_at=?,result=COALESCE(?,result) WHERE id=?",
                   (state, int(time.time()), json.dumps(result) if result is not None else None, run_id))


def audit(run_id: str | None, actor: str, action: str, status: str) -> None:
    with closing(storage.connection()) as db, db:
        db.execute("INSERT INTO run_events(run_id,actor,action,status,created_at) VALUES(?,?,?,?,?)",
                   (run_id, actor, action, status, int(time.time())))


def link_approval(run_id: str | None, token: str) -> None:
    if run_id:
        with closing(storage.connection()) as db, db:
            db.execute("INSERT INTO run_approvals VALUES(?,?)", (token, run_id))


def approval_run(token: str) -> str | None:
    with closing(storage.connection()) as db:
        row = db.execute("SELECT run_id FROM run_approvals WHERE approval_id=?", (token,)).fetchone()
        return row["run_id"] if row else None


def finish_approval(token: str, status: str, result: str) -> None:
    run_id = approval_run(token)
    if run_id:
        with closing(storage.connection()) as db, db:
            row = db.execute("SELECT session_id FROM runs WHERE id=?", (run_id,)).fetchone()
        payload = {"type": "done", "answer": result, "session_id": row["session_id"], "pending_approval": None}
        transition(run_id, {"approved": "COMPLETED", "failed": "FAILED", "rejected": "CANCELLED"}[status], payload)


def recover() -> None:
    with closing(storage.connection()) as db, db:
        db.execute("UPDATE runs SET state='CANCELLED',updated_at=? WHERE state IN ('RECEIVED','PLANNING','EXECUTING','RESUMING')",
                   (int(time.time()),))
        db.execute("UPDATE approvals SET status='failed', result='Execution was interrupted. Verify the actual outcome before retrying.' "
                   "WHERE status='processing'")
        db.execute("UPDATE mail_drafts SET status='unconfirmed' WHERE status='sending'")


def snapshot() -> dict:
    with closing(storage.connection()) as db:
        return {"runs": [dict(row) for row in db.execute(
                    "SELECT id,actor,session_id,state,created_at,updated_at FROM runs ORDER BY updated_at DESC LIMIT 30")],
                "events": [dict(row) for row in db.execute(
                    "SELECT run_id,actor,action,status,created_at FROM run_events ORDER BY id DESC LIMIT 60")],
                "pending": db.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]}
