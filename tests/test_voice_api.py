import json
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import voice
from balaji import ollama_client, voice_api


WAV_HEADER = b"RIFF\x24\x00\x00\x00WAVE"


@pytest.fixture
def client():
    with TestClient(server.app) as test_client:
        yield test_client


@pytest.fixture
def audio_models(monkeypatch):
    def convert(source, destination, *, input_format, max_seconds):
        assert source.read_bytes().startswith(WAV_HEADER)
        assert input_format == "wav"
        assert max_seconds == 60
        with wave.open(str(destination), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 160)

    monkeypatch.setattr(voice, "convert_audio", convert)


def upload(client, data=WAV_HEADER, mime="audio/wav", name="../recording.wav"):
    return client.post("/api/voice/transcribe", files={"file": (name, data, mime)})


def test_transcription_uses_unique_private_workspaces(client, audio_models, monkeypatch):
    paths = []

    def transcribe(wav, output):
        assert wav.is_file()
        paths.append(output.parent)
        output.with_suffix(".txt").write_text("Balaji, explain AI agents.", encoding="utf-8")
        return output.with_suffix(".txt").read_text(encoding="utf-8")

    monkeypatch.setattr(voice, "transcribe", transcribe)
    for _ in range(2):
        response = upload(client)
        assert response.status_code == 200
        assert response.json() == {"text": "Balaji, explain AI agents."}
    assert len(set(paths)) == 2
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize(("data", "mime", "status"), [
    (b"", "audio/wav", 422),
    (b"not audio", "audio/webm", 415),
    (WAV_HEADER, "audio/mp4", 415),
    (WAV_HEADER, "text/plain", 415),
])
def test_invalid_uploads_never_start_voice_workers(client, monkeypatch, data, mime, status):
    def unexpected(*args):
        pytest.fail("Invalid upload reached speech processing")

    monkeypatch.setattr(voice_api, "run_speech", unexpected)
    assert upload(client, data, mime).status_code == status


def test_upload_limits_file_and_chunked_multipart_body(client, monkeypatch):
    monkeypatch.setattr(voice_api, "MAX_AUDIO_BYTES", len(WAV_HEADER) - 1)
    assert upload(client).status_code == 413
    assert client.post("/api/voice/transcribe", json={"file": "oops"}).status_code == 415
    assert client.post("/api/voice/transcribe", files={"wrong": ("test.wav", WAV_HEADER)}).status_code == 400

    start = (b'--balaji\r\nContent-Disposition: form-data; name="file"; filename="test.wav"\r\n'
             b'Content-Type: audio/wav\r\n\r\n')
    monkeypatch.setattr(voice_api, "MAX_MULTIPART_BYTES", len(start) + 10)
    response = client.post(
        "/api/voice/transcribe", content=iter([start, WAV_HEADER, b"\r\n--balaji--\r\n"]),
        headers={"Content-Type": "multipart/form-data; boundary=balaji"},
    )
    assert response.status_code == 413


def test_invalid_container_is_reported_and_cleaned_up(client, monkeypatch):
    paths = []

    def decode(source, destination, **options):
        paths.append(source.parent)
        raise voice.VoiceProcessError("WAV conversion failed")

    monkeypatch.setattr(voice, "convert_audio", decode)
    response = upload(client)
    assert response.status_code == 422
    assert "decode" in response.json()["detail"]
    assert paths and not paths[0].exists()


@pytest.mark.parametrize("transcript", ["", "[BLANK_AUDIO]"])
def test_empty_transcription(client, audio_models, monkeypatch, transcript):
    monkeypatch.setattr(voice, "transcribe", lambda *_: transcript)
    response = upload(client)
    assert response.status_code == 422
    assert "No speech" in response.json()["detail"]


def test_speak_uses_local_configuration_and_returns_wav(client, monkeypatch, tmp_path):
    model = tmp_path / "test.onnx"
    model.touch()
    config = tmp_path / "test.onnx.json"
    config.touch()
    monkeypatch.setattr(voice, "VOICE_DIR", tmp_path)
    monkeypatch.setattr(voice, "PIPER_VOICE", "test")
    outputs = []

    def execute(args, *, label, input_text):
        assert args[1:3] == ["-m", "piper"]
        assert args[args.index("-m", 2) + 1] == str(model)
        assert args[args.index("--config") + 1] == str(config)
        assert input_text == "Hello BALAJI\n"
        destination = args[args.index("-f") + 1]
        outputs.append(destination)
        with wave.open(destination, "wb") as audio:
            audio.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 160)

    monkeypatch.setattr(voice, "execute", execute)
    response = client.post("/api/voice/speak", json={"text": " Hello  BALAJI "})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF" and response.content[8:12] == b"WAVE"
    assert outputs and not Path(outputs[0]).parent.exists()


@pytest.mark.parametrize("text", ["", "  ", "\0", "x" * (voice.MAX_SPEECH_CHARS + 1)])
def test_invalid_speech_text(client, text):
    assert client.post("/api/voice/speak", json={"text": text}).status_code == 422


def test_missing_speech_models(client, audio_models, monkeypatch, tmp_path):
    binary = tmp_path / "whisper-cli"
    binary.touch()
    monkeypatch.setattr(voice, "WHISPER_BIN", binary)
    monkeypatch.setattr(voice, "WHISPER_MODEL", tmp_path / "missing.bin")
    monkeypatch.setattr(voice, "VOICE_DIR", tmp_path)
    response = upload(client)
    assert response.status_code == 503
    assert "Whisper model missing" in response.json()["detail"]
    response = client.post("/api/voice/speak", json={"text": "Hello"})
    assert response.status_code == 503
    assert "Piper voice files missing" in response.json()["detail"]


def test_transcript_uses_streaming_chat_and_explicit_approval(client, audio_models, monkeypatch):
    transcript = "Remember that I prefer offline software"
    monkeypatch.setattr(voice, "transcribe", lambda *_: transcript)

    def model(messages, tools):
        assert messages[-1]["content"] == transcript
        yield {"content": "I can remember that."}
        yield {"tool_calls": [{"function": {"name": "remember_fact",
                                           "arguments": {"fact": "I prefer offline software"}}}]}

    monkeypatch.setattr(ollama_client, "chat_stream", model)
    spoken = upload(client).json()
    response = client.post("/api/chat/stream", json={"message": spoken["text"], "session_id": "voice-web"})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event["type"] for event in events] == ["token", "tool", "done"]
    pending = events[-1]["pending_approval"]
    assert client.get("/api/memories").json() == []
    assert client.get("/api/history/voice-web").json()[0] == {"role": "user", "content": transcript}
    response = client.post(f'/api/approvals/{pending["id"]}', json={"approved": True})
    assert response.status_code == 200
    assert client.get("/api/memories").json()[0]["fact"] == "I prefer offline software"
