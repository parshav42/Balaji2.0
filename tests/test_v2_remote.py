from unittest.mock import Mock
import json
import sqlite3
import time

from fastapi.testclient import TestClient

import server
from balaji import access, automation, config, desktop, ollama_client, storage, tools


REMOTE = "https://laptop.test-tailnet.ts.net"
LOCAL = "http://127.0.0.1:8000"


def local_session(client):
    response = client.post("/api/access/enroll", headers={"Origin": LOCAL}, json={
        "code": access.issue_code("owner"), "name": "Laptop", "platform": "Linux"
    })
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]


def test_private_phone_pair_and_remote_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REMOTE_HOST", "laptop.test-tailnet.ts.net")
    monkeypatch.setenv("DISPLAY", ":99")
    projects = tmp_path / "projects.json"
    approved = tmp_path / "Agent project"
    approved.mkdir()
    projects.write_text(json.dumps({"AI project": str(approved)}))
    monkeypatch.setattr(config, "PROJECTS_FILE", str(projects))
    executable = tmp_path / "code"
    executable.touch()
    executable.chmod(0o700)
    monkeypatch.setattr(desktop, "APPS", {"vscode": (str(executable),)})
    launch = Mock()
    launch.return_value.wait.return_value = 0
    monkeypatch.setattr(desktop.subprocess, "Popen", launch)
    phrase = "Open AI project in VS Code"

    def model(messages, schemas):
        assert messages[-1]["content"] == phrase
        yield {"tool_calls": [{"function": {"name": "launch_app",
                                            "arguments": {"app": "vscode", "project_name": "AI project"}}}]}

    monkeypatch.setattr(ollama_client, "chat_stream", model)
    remote = {"Origin": REMOTE, "Idempotency-Key": "open-project-test"}
    local = {"Origin": LOCAL}
    body = {"message": phrase, "session_id": "phone"}
    with TestClient(server.app, base_url=LOCAL) as owner, TestClient(server.app, base_url=REMOTE) as phone:
        assert owner.get("/api/tasks").status_code == phone.get("/api/tasks").status_code == 401
        assert phone.get("/docs").status_code == 404
        assert phone.get("/manifest.webmanifest").status_code == 200
        assert phone.post("/api/chat/stream", headers=remote, json=body).status_code == 401
        assert phone.post("/api/access/enroll", headers=remote, json={
            "code": access.issue_code("owner"), "name": "Phone"
        }).status_code == 400
        local_session(owner)
        invite = owner.post("/api/access/invite", headers=local).json()["code"]
        attempt = phone.post("/api/access/enroll", headers=remote, json={
            "code": invite, "name": "My phone", "platform": "Android"
        })
        assert attempt.json()["status"] == "pending"
        assert "Secure" in attempt.headers["set-cookie"]
        assert phone.post("/api/chat/stream", headers=remote, json=body).status_code == 401
        phone_id = phone.get("/api/access").json()["device"]["id"]
        assert owner.post("/api/access/devices/" + phone_id, headers=local, json={"allow": True}).status_code == 200
        assert phone.post("/api/access/invite", headers=remote).status_code == 403
        assert phone.post("/api/chat/stream", json=body).status_code == 403
        assert phone.post("/api/chat/stream", headers={"Origin": "https://other.example"}, json=body).status_code == 403
        assert phone.get("/api/tasks").status_code == 200
        reply = phone.post("/api/chat/stream", headers=remote, json=body)
        events = [json.loads(line[6:]) for line in reply.text.splitlines() if line.startswith("data: ")]
        pending = events[-1]["pending_approval"]
        assert pending["tool_name"] == "launch_app"
        assert owner.get("/api/approvals").json()[0]["id"] == pending["id"]
        assert launch.call_count == 0
        token = "/api/approvals/" + pending["id"]
        for _ in range(2):
            outcome = phone.post(token, headers=remote, json={"approved": True}).json()
            assert outcome["status"] == "approved"
        assert launch.call_count == 1
        assert launch.call_args.args[0][-1] == str(approved)
        assert phone.get("/api/activity").json()["runs"][0]["state"] == "COMPLETED"
        duplicate = phone.post("/api/chat/stream", headers=remote, json=body)
        assert "accepted the launch request" in duplicate.text
        assert launch.call_count == 1
        assert phone.post("/api/chat/stream", headers=remote, json={"message": "different"}).status_code == 409
        cookie = phone.cookies.get(access.COOKIE)
        with TestClient(server.app, base_url=REMOTE) as reconnected:
            reconnected.cookies.set(access.COOKIE, cookie)
            assert reconnected.get("/api/tasks").status_code == 200
        assert owner.post("/api/access/devices/" + phone_id, headers=local, json={"allow": False}).status_code == 200
        assert phone.get("/api/tasks").status_code == 401


def test_device_code_expiry_and_reuse(monkeypatch):
    monkeypatch.setattr(config, "REMOTE_HOST", "laptop.test-tailnet.ts.net")
    with TestClient(server.app, base_url=REMOTE) as phone:
        code = access.issue_code("device")
        headers = {"Origin": REMOTE}
        with storage.connection() as db:
            db.execute("UPDATE access_codes SET expires_at=0 WHERE secret_hash=?", (access.digest(code),))
        assert phone.post("/api/access/enroll", headers=headers, json={"code": code, "name": "Phone"}).status_code == 400
        fresh = access.issue_code("device")
        assert phone.post("/api/access/enroll", headers=headers, json={"code": fresh, "name": "Phone"}).status_code == 200
        assert phone.post("/api/access/enroll", headers=headers, json={"code": fresh, "name": "Other"}).status_code == 400


def test_files_are_confined_and_written_only_once(monkeypatch, tmp_path):
    location = tmp_path / "configured"
    location.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("Outside")
    (location / "link.md").symlink_to(outside)
    (location / ".env").write_text("Credentials")
    projects = tmp_path / "projects.json"
    projects.write_text(json.dumps({"AI": str(location)}))
    monkeypatch.setattr(config, "PROJECTS_FILE", str(projects))
    args = {"project_name": "AI", "path": "report.md", "content": "Verified work report"}
    assert tools.invoke("write_project_note", args).status == "failed"
    assert not (location / "report.md").exists()
    assert tools.invoke("write_project_note", args, authorized=True).status == "succeeded"
    assert tools.invoke("write_project_note", args | {"content": "Changed"}, authorized=True).status == "failed"
    assert (location / "report.md").read_text() == "Verified work report"
    for path in ("../outside.md", "link.md", ".env", str(outside)):
        assert tools.invoke("read_project_file", {"project_name": "AI", "path": path}).status == "failed"
    assert desktop.search_project_files("AI", "report") == "report.md"
    assert "link.md" not in desktop.search_project_files("AI", "link")
    with TestClient(server.app) as client:
        assert client.get("/api/files/download", params={"project": "AI", "path": "report.md"}).text == "Verified work report"
        assert client.get("/api/files/download", params={"project": "AI", "path": "link.md"}).status_code == 404


def test_scheduled_reminder_survives_restart_and_is_idempotent():
    due = int(time.time()) + 600
    zone = "Asia/Kolkata"
    from datetime import datetime
    from zoneinfo import ZoneInfo
    text = datetime.fromtimestamp(due, ZoneInfo(zone)).isoformat()
    job = automation.schedule("Practice", text, "daily", zone)
    storage.init_db()
    automation.tick(due + 1)
    automation.tick(due + 1)
    snapshot = automation.snapshot()
    assert snapshot["jobs"][0]["id"] == job
    assert snapshot["jobs"][0]["next_run"] == due + 86400
    assert [row["message"] for row in snapshot["events"]] == ["Practice"]


def test_migration_makes_private_snapshot_and_preserves_existing_notes(monkeypatch, tmp_path):
    path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, fact TEXT NOT NULL, created_at TEXT NOT NULL)")
        db.execute("INSERT INTO notes VALUES(1,'Old note','2025-01-01')")
    backup = storage.init_db()
    assert backup.is_dir() and (backup.stat().st_mode & 0o077) == 0
    with sqlite3.connect(backup / path.name) as original:
        assert original.execute("SELECT fact FROM notes").fetchone() == ("Old note",)
    assert storage.notes()[0]["fact"] == "Old note"
    assert storage.init_db() is None


def test_memory_controls_retain_legacy_notes_and_block_secret_edits():
    note = storage.add_note("A preference")
    with TestClient(server.app) as client:
        assert client.put(f"/api/memory/{note}", json={"fact": "Call me Sam", "category": "preferences"}).status_code == 200
        assert client.put(f"/api/memory/{note}", json={"fact": "password is secret"}).status_code == 422
        assert client.get("/api/memory", params={"q": "sam"}).json()["notes"][0]["category"] == "preferences"
        assert client.post("/api/memory/categories/preferences", json={"enabled": False}).status_code == 200
        assert client.get("/api/memories").json() == []
        assert client.delete(f"/api/memory/{note}").status_code == 200


def test_voice_chat_can_prepare_message_without_launching_or_sending(monkeypatch):
    draft = {"channel": "sms", "recipient": "+15555550100", "body": "I'm on the way."}

    def model(messages, schemas):
        if messages[-1]["role"] == "tool":
            assert "Prepared" in messages[-1]["content"]
            yield {"content": "You can review the draft in Messages."}
        else:
            yield {"tool_calls": [{"function": {"name": "prepare_message", "arguments": draft}}]}

    monkeypatch.setattr(ollama_client, "chat_stream", model)
    with TestClient(server.app) as client:
        result = client.post("/api/chat/stream", json={"message": "Prepare an SMS"}).text
        events = [json.loads(line[6:]) for line in result.splitlines() if line.startswith("data: ")]
        assert events[-1]["prepared_message"] == draft
        assert events[-1]["pending_approval"] is None
