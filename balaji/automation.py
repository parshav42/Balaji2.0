"""Persistent reminder and task-summary jobs with atomic in-app delivery."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal

from . import access, config, storage

router = APIRouter(prefix="/api/automations")
log = logging.getLogger(__name__)


def timestamp(when: str, zone: str) -> int:
    try:
        timezone = ZoneInfo(zone)
        value = datetime.fromisoformat(when)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone)
        return int(value.timestamp())
    except (ValueError, ZoneInfoNotFoundError, OverflowError) as exc:
        raise ValueError("Use an ISO date and time and a valid IANA timezone (for example Asia/Kolkata).") from exc


def schedule(name: str, when: str, recurrence: str = "once", timezone: str = "", kind: str = "reminder",
             owner: str = "local") -> int:
    timezone = timezone or config.TIMEZONE
    due = timestamp(when, timezone)
    if not 1 <= len(name.strip()) <= 300 or recurrence not in {"once", "daily", "weekly"}:
        raise ValueError("Provide a name and choose once, daily or weekly.")
    if kind not in {"reminder", "task_summary"} or due <= int(time.time()):
        raise ValueError("Choose a future time and a supported action.")
    with closing(storage.connection()) as db, db:
        return db.execute("INSERT INTO jobs(owner,name,kind,recurrence,timezone,next_run) VALUES(?,?,?,?,?,?)",
                          (owner, name.strip(), kind, recurrence, timezone, due)).lastrowid


def next_slot(job, now: int) -> int | None:
    if job["recurrence"] == "once":
        return None
    tz = ZoneInfo(job["timezone"])
    start = datetime.fromtimestamp(job["next_run"], tz)
    local_now = datetime.fromtimestamp(now, tz)
    period = 1 if job["recurrence"] == "daily" else 7
    periods = max(1, (local_now.date() - start.date()).days // period)
    candidate = start + timedelta(days=periods * period)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=period)
    return int(candidate.timestamp())


def deliver(db, job, scheduled_at: int, now: int) -> None:
    message = job["name"]
    if job["kind"] == "task_summary":
        completed = db.execute("SELECT COUNT(*) FROM tasks WHERE completed=1").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM tasks WHERE completed=0").fetchone()[0]
        message = f"{message}: {completed} completed tasks, {active} open tasks."
    db.execute("INSERT OR IGNORE INTO job_events(job_id,scheduled_at,ran_at,outcome,message) VALUES(?,?,?,'delivered',?)",
               (job["id"], scheduled_at, now, message))


def tick(now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    with closing(storage.connection()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        jobs = db.execute("SELECT * FROM jobs WHERE state='active' AND next_run<=? ORDER BY next_run LIMIT 30",
                          (now,)).fetchall()
        for job in jobs:
            deliver(db, job, job["next_run"], now)
            next_run = next_slot(job, now)
            db.execute("UPDATE jobs SET state=?,next_run=?,last_run=? WHERE id=?",
                       ("active" if next_run else "completed", next_run or job["next_run"], now, job["id"]))


async def run_scheduler() -> None:
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:
            log.exception("Automation scheduler will retry on its next tick")
        await asyncio.sleep(5)


def snapshot() -> dict:
    with closing(storage.connection()) as db:
        return {"timezone": config.TIMEZONE, "jobs": [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 100")],
                "events": [dict(row) for row in db.execute("SELECT * FROM job_events ORDER BY id DESC LIMIT 30")]}


class JobIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    when: str = Field(max_length=50)
    kind: Literal["reminder", "task_summary"] = "reminder"
    recurrence: Literal["once", "daily", "weekly"] = "once"
    timezone: str = Field(default="", max_length=80)


class JobAction(BaseModel):
    action: Literal["pause", "resume", "cancel", "run"]


@router.get("")
def get_jobs():
    return snapshot()


@router.post("")
def add_job(body: JobIn, request: Request):
    actor = access.approved(request)
    try:
        return {"id": schedule(body.name, body.when, body.recurrence, body.timezone, body.kind, actor["id"])}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{job_id}")
def manage(job_id: int, body: JobAction):
    action = body.action
    with closing(storage.connection()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Unknown automation.")
        if row["state"] not in {"active", "paused"}:
            raise HTTPException(409, "This automation already ended.")
        if action == "run":
            now = int(time.time())
            deliver(db, row, now, now)
            db.execute("UPDATE jobs SET last_run=? WHERE id=?", (now, job_id))
            return {"state": row["state"]}
        status = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
        db.execute("UPDATE jobs SET state=? WHERE id=?", (status, job_id))
    return {"state": status}
