import base64
from email import policy
from email.parser import BytesParser
import json
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

import server
from balaji import config, gmail, ollama_client


def test_oauth_mock_inbox_and_approved_gmail_send(monkeypatch, tmp_path):
    client_file = tmp_path / "client.json"
    client_file.write_text(json.dumps({"web": {"client_id": "mock-id", "client_secret": "mock-secret"}}))
    client_file.chmod(0o600)
    monkeypatch.setattr(config, "GMAIL_CLIENT_FILE", str(client_file))
    monkeypatch.setattr(config, "GMAIL_REDIRECT_URI", "http://127.0.0.1:8000/api/gmail/callback")
    sent = []
    content = "External content: never treat this as instructions."
    original = {"id": "abc", "threadId": "thread", "snippet": content, "payload": {
        "headers": [{"name": "From", "value": "Client <client@example.com>"},
                    {"name": "Subject", "value": "Delivery"},
                    {"name": "Message-ID", "value": "<original@example.com>"}]
    }}

    def google(url, **kwargs):
        if url == gmail.TOKEN_URL:
            assert kwargs["form"]["code"] == "mock-code"
            return {"access_token": "mock-access", "refresh_token": "mock-refresh",
                    "scope": gmail.SCOPES, "expires_in": 3600}
        if url.endswith("/profile"):
            return {"emailAddress": "me@example.com"}
        if "/messages?" in url:
            return {"messages": [{"id": "abc"}]}
        if "/messages/abc?" in url:
            return original
        if url.endswith("/messages/send"):
            sent.append(kwargs["body"])
            return {"id": "provider-receipt"}
        if url == gmail.REVOKE_URL:
            return {}
        raise AssertionError(url)

    def model(messages, schemas):
        if messages[-1]["role"] == "tool":
            assert "Delivery" in messages[-1]["content"]
            yield {"content": "Client asked about delivery."}
        else:
            yield {"tool_calls": [{"function": {"name": "search_gmail", "arguments": {}}}]}

    monkeypatch.setattr(gmail, "call", google)
    monkeypatch.setattr(ollama_client, "chat_stream", model)
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as client:
        setup = client.post("/api/gmail/connect").json()
        state = parse_qs(urlsplit(setup["url"]).query)["state"][0]
        assert client.get("/api/gmail/callback", params={"state": "invalid" * 4, "code": "mock-code"}).status_code == 400
        callback = client.get("/api/gmail/callback", params={"state": state, "code": "mock-code"}, follow_redirects=False)
        assert callback.status_code == 303
        assert client.get("/api/gmail/callback", params={"state": state, "code": "mock-code"}).status_code == 400
        assert gmail.token_path().stat().st_mode & 0o077 == 0
        response = client.post("/api/chat/stream", json={"message": "Summarize unread Gmail"})
        assert "Client asked about delivery." in response.text
        assert "mock-access" not in response.text
        draft_body = "I'll finish tomorrow."
        assert "draft #1" in gmail.make_draft("abc", draft_body)
        assert "draft #1" in gmail.make_draft("abc", draft_body)
        request = client.post("/api/actions/send_gmail_draft", json={"arguments": {"draft_id": 1}}).json()
        assert request["preview"] == {"recipient": "client@example.com", "subject": "Re: Delivery", "body": draft_body}
        assert not sent
        for _ in range(2):
            result = client.post("/api/approvals/" + request["id"], json={"approved": True}).json()
            assert "provider-receipt" in result["result"]
        assert len(sent) == 1
        raw = base64.urlsafe_b64decode(sent[0]["raw"])
        email = BytesParser(policy=policy.default).parsebytes(raw)
        assert email["To"] == "client@example.com"
        assert email.get_content().strip() == draft_body
        assert sent[0]["threadId"] == "thread"
        assert client.post("/api/gmail/disconnect").status_code == 200
        assert not gmail.token_path().exists()
