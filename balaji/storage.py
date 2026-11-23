"""SQLite persistence for notes, tasks, conversations, approvals, and RAG chunks."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from . import config


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connection() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> Path | None:
    from .maintenance import backup_before_upgrade
    backup = backup_before_upgrade()
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, fact TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT NOT NULL,
          completed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('user','assistant')),
          content TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
        CREATE TABLE IF NOT EXISTS documents (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chunks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          page INTEGER, chunk_no INTEGER NOT NULL, content TEXT NOT NULL,
          embedding TEXT);
        CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
        CREATE TABLE IF NOT EXISTS approvals (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool_name TEXT NOT NULL,
          arguments TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          result TEXT, created_at TEXT NOT NULL);
        """)
        db.executescript(Path(__file__).with_name("v2.sql").read_text(encoding="utf-8"))
    return backup


def add_message(session: str, role: str, content: str) -> None:
    if role not in ("user", "assistant"):
        raise ValueError("Invalid conversation role")
    with connection() as db:
        db.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                   (session, role, content, now()))


def add_turn(session: str, question: str, answer: str) -> None:
    timestamp = now()
    with connection() as db:
        db.executemany("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                       [(session, "user", question, timestamp), (session, "assistant", answer, timestamp)])


def history(session: str, limit: int = 16) -> list[dict]:
    with connection() as db:
        rows = db.execute("SELECT role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                          (session, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear_history(session: str) -> None:
    with connection() as db:
        db.execute("DELETE FROM messages WHERE session_id=?", (session,))


def memory_enabled(db: sqlite3.Connection, category: str) -> bool:
    row = db.execute("SELECT enabled FROM memory_settings WHERE category=?", (category,)).fetchone()
    return bool(row[0]) if row else True


def add_note(fact: str, category: str = "saved_notes") -> int:
    with connection() as db:
        if not memory_enabled(db, category):
            raise ValueError("This memory category is disabled.")
        cur = db.execute("INSERT INTO notes(fact,created_at) VALUES(?,?)", (fact, now()))
        note_id = int(cur.lastrowid)
        db.execute("INSERT INTO memory_categories VALUES(?,?)", (note_id, category))
        return note_id


def notes(keyword: str = "", limit: int = 30) -> list[dict]:
    with connection() as db:
        rows = db.execute("""SELECT n.id,n.fact,n.created_at FROM notes n
                             LEFT JOIN memory_categories c ON c.note_id=n.id
                             LEFT JOIN memory_settings s ON s.category=COALESCE(c.category,'saved_notes')
                             WHERE n.fact LIKE ? AND COALESCE(s.enabled,1)=1 ORDER BY n.id DESC LIMIT ?""",
                          (f"%{keyword}%", limit)).fetchall()
    return [dict(r) for r in rows]


def add_task(description: str) -> int:
    with connection() as db:
        cur = db.execute("INSERT INTO tasks(description,created_at) VALUES(?,?)", (description, now()))
        return int(cur.lastrowid)


def tasks(include_done: bool = False) -> list[dict]:
    with connection() as db:
        sql = "SELECT id,description,completed,created_at FROM tasks"
        if not include_done:
            sql += " WHERE completed=0"
        rows = db.execute(sql + " ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


def finish_task(task_id: int) -> bool:
    with connection() as db:
        cur = db.execute("UPDATE tasks SET completed=1 WHERE id=? AND completed=0", (task_id,))
        return cur.rowcount > 0


def add_document(name: str, chunks: list[tuple[int | None, int, str, list | None]]) -> int:
    with connection() as db:
        cur = db.execute("INSERT INTO documents(name,created_at) VALUES(?,?)", (name, now()))
        doc_id = int(cur.lastrowid)
        db.executemany("INSERT INTO chunks(document_id,page,chunk_no,content,embedding) VALUES(?,?,?,?,?)",
                       [(doc_id, page, number, content, json.dumps(embedding) if embedding is not None else None)
                        for page, number, content, embedding in chunks])
        return doc_id


def documents() -> list[dict]:
    with connection() as db:
        rows = db.execute("""SELECT d.id,d.name,d.created_at,COUNT(c.id) as chunks
                             FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                             GROUP BY d.id ORDER BY d.id DESC""").fetchall()
    return [dict(r) for r in rows]


def all_chunks() -> list[dict]:
    with connection() as db:
        rows = db.execute("""SELECT c.id,c.page,c.chunk_no,c.content,c.embedding,d.name
                             FROM chunks c JOIN documents d ON d.id=c.document_id
                             ORDER BY c.id DESC LIMIT 3000""").fetchall()
    return [dict(r) for r in rows]


def create_approval(token: str, session: str, name: str, args: dict) -> None:
    with connection() as db:
        db.execute("INSERT INTO approvals(id,session_id,tool_name,arguments,created_at) VALUES(?,?,?,?,?)",
                   (token, session, name, json.dumps(args), now()))


def pending_approvals(session: str | None) -> list[dict]:
    with connection() as db:
        rows = db.execute("SELECT * FROM approvals WHERE (? IS NULL OR session_id=?) AND status='pending' "
                          "ORDER BY created_at LIMIT 100", (session, session)).fetchall()
    return [dict(r) | {"arguments": json.loads(r["arguments"])} for r in rows]


def claim_approval(token: str) -> dict | None:
    """Claim an approval exactly once; do not hold a lock while executing tools."""
    with connection() as db:
        row = db.execute("SELECT * FROM approvals WHERE id=?", (token,)).fetchone()
        if row is None:
            return None
        if row["status"] == "pending":
            changed = db.execute(
                "UPDATE approvals SET status='processing' WHERE id=? AND status='pending'", (token,)
            ).rowcount
            if not changed:
                return {"id": token, "status": "processing", "result": None}
            return {"id": token, "status": "processing", "session_id": row["session_id"],
                    "tool_name": row["tool_name"], "arguments": json.loads(row["arguments"])}
        return {"id": token, "status": row["status"], "result": row["result"]}


def finish_approval(token: str, status: str, result: str) -> None:
    with connection() as db:
        db.execute("UPDATE approvals SET status=?, result=? WHERE id=? AND status='processing'",
                   (status, result, token))


def reject_approval(token: str) -> dict | None:
    with connection() as db:
        row = db.execute("SELECT * FROM approvals WHERE id=?", (token,)).fetchone()
        if row is None:
            return None
        if row["status"] == "pending":
            db.execute("UPDATE approvals SET status='rejected',result=? WHERE id=? AND status='pending'",
                       ("Request rejected by user.", token))
            return {"id": token, "status": "rejected", "result": "Request rejected by user.", "session_id": row["session_id"]}
        return {"id": token, "status": row["status"], "result": row["result"]}
