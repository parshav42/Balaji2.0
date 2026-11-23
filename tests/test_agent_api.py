import json
from contextlib import nullcontext

import anyio
from fastapi.testclient import TestClient
import pytest
import server
from balaji import agent, storage, ollama_client


def fixed_reply(messages, tools):
    return {'role': 'assistant', 'content': 'Hello from local model', 'tool_calls': []}


def create_tool_reply(messages, tools):
    if any(m.get('role') == 'tool' for m in messages):
        return {'role': 'assistant', 'content': 'Action completed or rejected', 'tool_calls': []}
    return {'role':'assistant','content':'',
            'tool_calls':[{'function':{'name':'create_task','arguments':{'description':'Study LangGraph'}}}]}


def test_simple_agent_response(monkeypatch):
    monkeypatch.setattr(ollama_client, 'chat', fixed_reply)
    ans = agent.ask('Hi','session1')
    assert ans['answer'] == 'Hello from local model'
    assert len(storage.history('session1')) == 2


def test_agent_approval_is_required_and_only_once(monkeypatch):
    monkeypatch.setattr(ollama_client, 'chat', create_tool_reply)
    ans = agent.ask('Create task', 's1')
    assert ans['pending_approval']
    assert storage.tasks() == []  # no side effects before approval
    token = ans['pending_approval']['id']
    first = agent.decide_approval(token, True)
    assert first['status'] == 'approved'
    assert len(storage.tasks()) == 1
    again = agent.decide_approval(token, True)
    assert again['status'] == 'approved'
    assert len(storage.tasks()) == 1  # cannot double-execute


def test_agent_rejection(monkeypatch):
    monkeypatch.setattr(ollama_client, 'chat', create_tool_reply)
    token = agent.ask('Create task','s1')['pending_approval']['id']
    rejected = agent.decide_approval(token, False)
    assert rejected['status'] == 'rejected'
    assert storage.tasks() == []


def test_terminal_approval_callback(monkeypatch):
    monkeypatch.setattr(ollama_client, 'chat', create_tool_reply)
    res = agent.ask('Create task','cli',lambda n,args:True)
    assert res['pending_approval'] is None
    assert len(storage.tasks()) == 1


def test_api_routes(monkeypatch):
    monkeypatch.setattr(ollama_client,'chat',fixed_reply)
    with TestClient(server.app) as client:
        assert client.get('/health').status_code == 200
        assert 'BALAJI' in client.get('/').text
        response = client.post('/api/chat', json={'message':'Hi','session_id':'web'})
        assert response.status_code == 200
        assert response.json()['answer'] == 'Hello from local model'
        assert client.get('/api/tasks').json() == []
        assert client.get('/api/history/web').status_code == 200


def test_api_chat_stream(monkeypatch):
    requests = []

    def open_stream(request, timeout):
        requests.append(json.loads(request.data))
        return nullcontext(iter([
            b'{"message":{"thinking":"Internal reasoning"},"done":false}\n',
            b'{"message":{"content":"Hello "},"done":false}\n',
            b'{"message":{"content":"from stream"},"done":false}\n',
            b'{"done":true}\n',
        ]))

    monkeypatch.setattr(ollama_client.request, 'urlopen', open_stream)
    with TestClient(server.app) as client:
        response = client.post('/api/chat/stream', json={'message':'Hi','session_id':'stream'})
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/event-stream')
        assert [json.loads(line[6:]) for line in response.text.splitlines() if line] == [
            {'type': 'token', 'text': 'Hello '},
            {'type': 'token', 'text': 'from stream'},
            {'type': 'done', 'answer': 'Hello from stream', 'session_id': 'stream', 'pending_approval': None},
        ]
        assert storage.history('stream')[-1]['content'] == 'Hello from stream'
    assert len(requests) == 1
    assert requests[0]['stream'] is True


def test_stream_yields_before_generation_finishes_and_waits_for_approval(monkeypatch):
    def stream(messages, tools):
        yield {'content': 'Let me check. '}
        assert storage.history('stream') == []
        yield create_tool_reply(messages, tools)

    monkeypatch.setattr(ollama_client, 'chat_stream', stream)
    events = agent.ask_stream('Create task', 'stream')
    assert next(events) == {'type': 'token', 'text': 'Let me check. '}
    assert next(events) == {'type': 'tool'}
    assert next(events)['pending_approval']['tool_name'] == 'create_task'
    assert storage.tasks() == []


def test_truncated_stream_reports_error_without_saving_partial_reply(monkeypatch):
    monkeypatch.setattr(ollama_client.request, 'urlopen', lambda *_args, **_kwargs:
                        nullcontext(iter([b'{"message":{"content":"Partial answer"},"done":false}\n'])))
    with TestClient(server.app) as client:
        response = client.post('/api/chat/stream', json={'message': 'Hi', 'session_id': 'stream'})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line]
    assert [event['type'] for event in events] == ['token', 'error']
    assert storage.history('stream') == []


def test_disconnected_chat_closes_ollama_stream(monkeypatch):
    closed = []

    def stream(messages, tools):
        try:
            yield {'content': 'Partial answer'}
            pytest.fail('A disconnected client must not advance the reply')
        finally:
            closed.append(True)

    monkeypatch.setattr(ollama_client, 'chat_stream', stream)

    async def disconnect():
        first_token = anyio.Event()
        response = server.chat_stream(server.ChatIn(message='Hi', session_id='stopped'))

        async def send(message):
            if message['type'] == 'http.response.body':
                assert b'Partial answer' in message['body']
                first_token.set()
                await anyio.sleep_forever()

        async def receive():
            await first_token.wait()
            return {'type': 'http.disconnect'}

        with anyio.fail_after(2):
            await response({'type': 'http', 'asgi': {'spec_version': '2.3'}}, receive, send)

    anyio.run(disconnect)
    assert closed == [True]
    assert storage.history('stopped') == []


def test_api_approval(monkeypatch):
    monkeypatch.setattr(ollama_client,'chat',create_tool_reply)
    with TestClient(server.app) as client:
        pending = client.post('/api/chat', json={'message':'Make a task','session_id':'web'}).json()['pending_approval']
        assert client.get('/api/tasks').json() == []
        resp = client.post('/api/approvals/'+pending['id'], json={'approved':True})
        assert resp.status_code == 200
        assert len(client.get('/api/tasks').json()) == 1


def test_document_upload(monkeypatch):
    with TestClient(server.app) as client:
        res = client.post('/api/documents/upload', files={'file':('manual.txt',b'Python integrates Ollama and LangGraph.', 'text/plain')})
        assert res.status_code == 200
        assert res.json()['chunks'] == 1
        assert client.get('/api/documents').json()[0]['name'] == 'manual.txt'
        bad = client.post('/api/documents/upload', files={'file':('bad.exe',b'test')})
        assert bad.status_code == 400


def test_api_invalid_input():
    with TestClient(server.app) as client:
        assert client.post('/api/chat', json={'message':'', 'session_id':'web'}).status_code == 422
        assert client.get('/api/history/not.valid').status_code == 400


def test_cross_origin_write_blocked():
    with TestClient(server.app) as client:
        response = client.post('/api/chat', headers={'Origin':'https://evil.example'}, json={'message':'Hi'})
        assert response.status_code == 403
