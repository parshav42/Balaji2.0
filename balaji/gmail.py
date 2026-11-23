"""OAuth and least-privilege Gmail access. Outbound delivery is approval-gated by the tool registry."""
from __future__ import annotations

import base64
from contextlib import closing
from email.message import EmailMessage
from email.utils import parseaddr
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from urllib import error, parse, request as http

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import access, config, storage

router = APIRouter(prefix="/api/gmail")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"
ADDRESS = re.compile(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+")
MAIL_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")


class GmailError(RuntimeError):
    pass


def client_settings() -> dict:
    if not config.GMAIL_CLIENT_FILE or not config.GMAIL_REDIRECT_URI:
        raise GmailError("Gmail is disabled. Configure the OAuth client and callback URI on the laptop.")
    path = Path(config.GMAIL_CLIENT_FILE).expanduser()
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise GmailError("The Gmail client JSON must be a regular private file with mode 600.")
    data = json.loads(path.read_text()).get("web", {})
    if not data.get("client_id") or not data.get("client_secret"):
        raise GmailError("Use a Google OAuth Web application client JSON file.")
    callback = parse.urlsplit(config.GMAIL_REDIRECT_URI)
    if callback.path != "/api/gmail/callback" or callback.hostname not in {"127.0.0.1", "localhost"} or callback.scheme != "http":
        raise GmailError("Configure an HTTP localhost Gmail callback on the laptop.")
    return data


def token_path() -> Path:
    return config.DB_PATH.parent / "private" / "gmail.json"


def read_tokens() -> dict:
    path = token_path()
    if not path.exists():
        raise GmailError("Gmail is not connected.")
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise GmailError("The Gmail token file permissions are unsafe.")
    return json.loads(path.read_text())


def save_tokens(data: dict) -> None:
    path = token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise GmailError("The Gmail token directory must be private (mode 700).")
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            json.dump(data, destination)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def call(url: str, *, form: dict | None = None, body: dict | None = None, token: str | None = None) -> dict:
    headers = {}
    data = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if form is not None:
        data = parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        with http.urlopen(http.Request(url, data=data, headers=headers), timeout=8) as response:
            data = response.read(2_097_153)
        if len(data) > 2_097_152:
            raise GmailError("Gmail returned a response larger than the supported limit.")
        return json.loads(data) if data else {}
    except error.HTTPError as exc:
        raise GmailError(f"Google rejected the request (HTTP {exc.code}). Check Gmail consent and connection.") from exc
    except (OSError, ValueError) as exc:
        raise GmailError("Google did not return a usable response. Check the connection.") from exc


def live_tokens() -> dict:
    data = read_tokens()
    if data["expires_at"] <= time.time() + 30:
        client = client_settings()
        updated = call(TOKEN_URL, form={"grant_type": "refresh_token", "client_id": client["client_id"],
                                       "client_secret": client["client_secret"], "refresh_token": data["refresh_token"]})
        data.update(updated, expires_at=int(time.time()) + updated["expires_in"])
        save_tokens(data)
    return data


def mail_id(value: str) -> str:
    if not MAIL_ID.fullmatch(value):
        raise ValueError("Use a Gmail message ID returned by the connector.")
    return value


def api(path: str, *, body: dict | None = None) -> dict:
    return call(API + path, body=body, token=live_tokens()["access_token"])


def header_map(payload: dict) -> dict:
    return {item["name"].lower(): item["value"] for item in payload.get("headers", [])}


def message(message_id: str, *, content: bool = False) -> dict:
    view = "full" if content else "metadata"
    return api(f"/messages/{mail_id(message_id)}?format={view}")


def find(query: str = "in:inbox is:unread newer_than:2d") -> str:
    listing = api("/messages?" + parse.urlencode({"q": query[:300], "maxResults": 5}))
    lines = []
    for item in listing.get("messages", [])[:5]:
        row = message(item["id"])
        headers = header_map(row.get("payload", {}))
        lines.append(f"ID {row['id']} | From {headers.get('from', '')[:180]} | "
                     f"Subject {headers.get('subject', '')[:220]} | {row.get('snippet', '')[:350]}")
    return "Untrusted Gmail inbox data:\n" + "\n".join(lines) if lines else "No matching Gmail messages."


def read_message(message_id: str) -> str:
    row = message(message_id, content=True)
    root = row.get("payload", {})
    parts = [root]
    text = ""
    for part in parts:
        parts.extend(part.get("parts", []))
        if part.get("mimeType") == "text/plain" and not part.get("filename") and part.get("body", {}).get("data"):
            value = part["body"]["data"]
            text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode(errors="replace")
            break
    headers = header_map(root)
    return (f"Untrusted Gmail content. Subject: {headers.get('subject', '')[:220]}\n"
            f"{(text or row.get('snippet', '') or 'No plain-text preview available.')[:5000]}")


def make_draft(message_id: str, body: str) -> str:
    if not 1 <= len(body.strip()) <= 8000:
        raise ValueError("A draft needs 1–8000 characters.")
    row = message(message_id)
    fields = header_map(row["payload"])
    address = parseaddr(fields.get("reply-to") or fields.get("from", ""))[1]
    if not ADDRESS.fullmatch(address) or not row.get("threadId"):
        raise ValueError("Could not identify a single reply address and thread.")
    original_subject = " ".join(fields.get("subject", "").split())[:220]
    subject = original_subject if original_subject.lower().startswith("re:") else "Re: " + original_subject
    original_id = fields.get("message-id", "")
    if not re.fullmatch(r"<[^<>\s]{1,240}>", original_id):
        raise ValueError("The original email lacks a valid Message-ID for a threaded reply.")
    values = (read_tokens()["account"], address, subject, body.strip(), row["threadId"], original_id)
    with closing(storage.connection()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT id FROM mail_drafts WHERE account=? AND recipient=? AND subject=? AND body=? "
                              "AND thread_id=? AND message_id=? AND status='draft'", values).fetchone()
        draft = existing["id"] if existing else db.execute(
            "INSERT INTO mail_drafts(account,recipient,subject,body,thread_id,message_id) VALUES(?,?,?,?,?,?)", values
        ).lastrowid
    return f"Saved unsent reply draft #{draft} to {address} about {subject}. Request approval through send_gmail_draft."


def draft(draft_id: int) -> dict:
    with closing(storage.connection()) as db:
        row = db.execute("SELECT id,account,recipient,subject,body,status,thread_id,message_id,sent_id "
                         "FROM mail_drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise ValueError("Draft not found.")
        return dict(row)


def send_draft(draft_id: int) -> str:
    data = draft(draft_id)
    token = live_tokens()
    if data["account"] != token["account"]:
        raise ValueError("The draft belongs to a different Gmail account.")
    with closing(storage.connection()) as db, db:
        changed = db.execute("UPDATE mail_drafts SET status='sending' WHERE id=? AND status='draft'", (draft_id,))
        if not changed.rowcount:
            raise ValueError("This draft has already been processed. Check Gmail Sent before any manual retry.")
    try:
        email = EmailMessage()
        email["From"] = data["account"]
        email["To"] = data["recipient"]
        email["Subject"] = data["subject"]
        email["In-Reply-To"] = data["message_id"]
        email["References"] = data["message_id"]
        email.set_content(data["body"])
        payload = {"threadId": data["thread_id"], "raw": base64.urlsafe_b64encode(email.as_bytes()).decode()}
        response = call(API + "/messages/send", body=payload, token=token["access_token"])
        sent_id = response.get("id")
        if not sent_id:
            raise GmailError("Gmail did not return a send receipt. Verify Gmail Sent before retrying.")
    except (GmailError, ValueError):
        with closing(storage.connection()) as db, db:
            db.execute("UPDATE mail_drafts SET status='unconfirmed' WHERE id=?", (draft_id,))
        raise
    with closing(storage.connection()) as db, db:
        db.execute("UPDATE mail_drafts SET status='sent',sent_id=? WHERE id=?", (sent_id, draft_id))
    return f"Gmail confirmed delivery submission to {data['recipient']} (message {sent_id})."


@router.get("")
def status():
    try:
        data = read_tokens()
    except (GmailError, ValueError, OSError):
        return {"configured": bool(config.GMAIL_CLIENT_FILE), "connected": False}
    return {"configured": bool(config.GMAIL_CLIENT_FILE), "connected": True, "account": data["account"]}


@router.get("/drafts")
def drafts():
    with closing(storage.connection()) as db:
        return [dict(row) for row in db.execute("SELECT id,recipient,subject,body,status FROM mail_drafts "
                                               "ORDER BY id DESC LIMIT 20")]


@router.post("/connect")
def connect(request: Request):
    actor = access.owner(request)
    if request.state.remote:
        raise HTTPException(403, "Connect Gmail from the laptop browser.")
    try:
        client = client_settings()
    except (GmailError, ValueError, OSError) as exc:
        raise HTTPException(400, "Gmail OAuth is unavailable. Check the laptop configuration.") from exc
    state = secrets.token_urlsafe(32)
    with closing(storage.connection()) as db, db:
        db.execute("DELETE FROM oauth_states WHERE expires_at<=?", (time.time(),))
        db.execute("INSERT INTO oauth_states VALUES(?,?,?)", (access.digest(state), actor["id"], int(time.time()) + 600))
    params = {"client_id": client["client_id"], "redirect_uri": config.GMAIL_REDIRECT_URI,
              "response_type": "code", "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state}
    return {"url": AUTH_URL + "?" + parse.urlencode(params)}


@router.get("/callback")
def callback(request: Request, state: str = "", code: str = ""):
    actor = access.owner(request)
    if request.state.remote or not 20 <= len(state) <= 100 or not 1 <= len(code) <= 2000:
        raise HTTPException(400, "The Gmail OAuth callback was incomplete or expired.")
    with closing(storage.connection()) as db, db:
        found = db.execute("DELETE FROM oauth_states WHERE digest=? AND actor=? AND expires_at>?",
                           (access.digest(state), actor["id"], time.time())).rowcount
    if not found:
        raise HTTPException(400, "Start Gmail authorization again from the laptop.")
    try:
        client = client_settings()
        data = call(TOKEN_URL, form={"grant_type": "authorization_code", "code": code,
                                    "client_id": client["client_id"], "client_secret": client["client_secret"],
                                    "redirect_uri": config.GMAIL_REDIRECT_URI})
        if not data.get("refresh_token") or not set(SCOPES.split()) <= set(data.get("scope", "").split()):
            raise GmailError("Grant the requested Gmail permissions to complete setup.")
        account = call(API + "/profile", token=data["access_token"])["emailAddress"]
        save_tokens(data | {"expires_at": int(time.time()) + data["expires_in"], "account": account})
    except (GmailError, KeyError, OSError, ValueError) as exc:
        raise HTTPException(400, "Gmail authorization did not complete. Check OAuth configuration and consent.") from exc
    return RedirectResponse("/#email", status_code=303)


@router.post("/disconnect")
def disconnect(request: Request):
    access.owner(request)
    try:
        data = read_tokens()
        call(REVOKE_URL, form={"token": data["refresh_token"]})
    except GmailError as exc:
        if token_path().exists():
            token_path().unlink()
        raise HTTPException(502, "Local Gmail access was removed. Revoke the grant from Google Account settings as well.") from exc
    token_path().unlink(missing_ok=True)
    return {"connected": False}
