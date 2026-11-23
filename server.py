"""Local-only FastAPI service + BALAJI browser dashboard."""
from __future__ import annotations
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, File, HTTPException, UploadFile, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from balaji import agent, storage, config, rag
from balaji.ollama_client import OllamaError

@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    yield

app = FastAPI(title="BALAJI Local Agent API", version="1.0.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

@app.middleware("http")
async def reject_cross_origin_writes(request: Request, call_next):
    """Protect local browser actions from cross-site requests with a foreign Origin."""
    origin = request.headers.get("origin")
    if origin and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port not in (8000, None):
            return JSONResponse(status_code=403, content={"detail": "Cross-origin writes are blocked"})
    return await call_next(request)
WEB_ROOT = Path(__file__).parent / "web"

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(default="web", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

class ApprovalIn(BaseModel):
    approved: bool

@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_ROOT / "index.html").read_text(encoding="utf-8")

@app.get("/health")
def health():
    return {"status": "ok", "model": config.MODEL, "embeddings": config.EMBED_MODEL or "keyword-only"}

@app.post("/api/chat")
def chat(inp: ChatIn):
    try:
        return agent.ask(inp.message, inp.session_id)
    except (OllamaError, ValueError) as exc:
        raise HTTPException(status_code=503 if isinstance(exc, OllamaError) else 400, detail=str(exc)) from exc

@app.post("/api/chat/stream")
def chat_stream(inp: ChatIn):
    import json

    def events():
        try:
            for event in agent.ask_stream(inp.message, inp.session_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except (OllamaError, ValueError) as exc:
            status = "503" if isinstance(exc, OllamaError) else "400"
            yield f"data: {json.dumps({'type':'error','status':status,'detail':str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/api/tasks")
def tasks():
    return storage.tasks(include_done=True)

@app.get("/api/memories")
def notes():
    return storage.notes()

@app.get("/api/history/{session_id}")
def history(session_id: str):
    if len(session_id) > 64 or not all(c.isalnum() or c in "_-" for c in session_id):
        raise HTTPException(status_code=400, detail="Invalid session")
    return storage.history(session_id, 100)

@app.get("/api/approvals/{session_id}")
def approvals(session_id: str):
    if len(session_id) > 64 or not all(c.isalnum() or c in "_-" for c in session_id):
        raise HTTPException(status_code=400, detail="Invalid session")
    return storage.pending_approvals(session_id)

@app.post("/api/approvals/{token}")
def approve(token: str, body: ApprovalIn):
    result = agent.decide_approval(token, body.approved)
    if result is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return result

@app.get("/api/documents")
def documents():
    return storage.documents()

@app.post("/api/documents/upload")
async def upload(file: UploadFile = File(...)):
    name = file.filename or ""
    if Path(name).suffix.lower() not in {".txt", ".md", ".pdf"}:
        raise HTTPException(status_code=400, detail="Only .txt, .md or .pdf files")
    contents = await file.read(config.MAX_FILE_BYTES + 1)
    try:
        return rag.ingest_bytes(name, contents)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
