"""Minimal Ollama HTTP client using only Python's standard library."""
from __future__ import annotations
import json
from collections.abc import Iterator
from http.client import HTTPException
from urllib import error, request
from . import config

class OllamaError(RuntimeError):
    pass


def _post(endpoint: str, payload: dict, timeout: int = 180) -> dict:
    config.validate_ollama_url()
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(config.OLLAMA_URL + endpoint, data=data,
                          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read(16 * 1024 * 1024))
    except error.HTTPError as exc:
        details = exc.read(500).decode("utf-8", errors="replace")
        raise OllamaError(f"Ollama HTTP {exc.code}: {details}") from exc
    except (error.URLError, TimeoutError) as exc:
        raise OllamaError("Cannot reach local Ollama. Run `ollama serve` or start its service; verify `ollama list`.") from exc


def chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    payload = {"model": config.MODEL, "messages": messages, "stream": False, "options": {"temperature": 0.2, "num_ctx": 4096}}
    if tools:
        payload["tools"] = tools
    result = _post("/api/chat", payload)
    if "message" not in result:
        raise OllamaError("Ollama returned no message")
    return result["message"]


def chat_stream(messages: list[dict], tools: list[dict] | None = None) -> Iterator[dict]:
    """Yield assistant message fragments from a single Ollama generation."""
    config.validate_ollama_url()
    payload = {"model": config.MODEL, "messages": messages, "stream": True, "options": {"temperature": 0.2, "num_ctx": 4096}}
    if tools:
        payload["tools"] = tools
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(config.OLLAMA_URL + "/api/chat", data=data,
                          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=180) as response:
            for raw_line in response:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise OllamaError("Ollama returned invalid streaming data") from exc
                if not isinstance(chunk, dict):
                    raise OllamaError("Ollama returned invalid streaming data")
                if chunk.get("error"):
                    raise OllamaError(str(chunk["error"]))
                message = chunk.get("message")
                if message is not None:
                    if not isinstance(message, dict):
                        raise OllamaError("Ollama returned an invalid streaming message")
                    yield message
                if chunk.get("done"):
                    return
    except error.HTTPError as exc:
        details = exc.read(500).decode("utf-8", errors="replace")
        raise OllamaError(f"Ollama HTTP {exc.code}: {details}") from exc
    except (OSError, HTTPException) as exc:
        raise OllamaError("The Ollama stream was interrupted. Check that Ollama is running and try again.") from exc
    raise OllamaError("Ollama closed the stream before completing its reply")


def embed(text: str) -> list[float]:
    if not config.EMBED_MODEL:
        return []
    result = _post("/api/embed", {"model": config.EMBED_MODEL, "input": text}, timeout=120)
    vectors = result.get("embeddings") or []
    return [float(v) for v in vectors[0]] if vectors else []
