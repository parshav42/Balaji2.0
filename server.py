"""Local-only FastAPI service + BALAJI browser dashboard."""
from __future__ import annotations
from contextlib import asynccontextmanager, suppress
import asyncio
import re
from urllib import request as http
import json
from pathlib import Path
from urllib.parse import urlsplit
from anyio import CancelScope, to_thread
from fastapi import FastAPI, File, HTTPException, UploadFile, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from balaji import access, activity, agent, automation, desktop, gmail, storage, config, rag, tools, workspace_api
from balaji.ollama_client import OllamaError
from balaji.voice_api import router as voice_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    activity.recover()
    scheduler = asyncio.create_task(automation.run_scheduler())
    try:
        yield
    finally:
        scheduler.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler


config.validate_remote_host()
app = FastAPI(title="BALAJI Local Agent API", version="2.0.0", lifespan=lifespan)
app.include_router(voice_router)
app.include_router(access.router)
app.include_router(automation.router)
app.include_router(gmail.router)
app.include_router(workspace_api.router)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "testserver"}
PUBLIC_ACCESS = {"/api/access", "/api/access/enroll", "/api/access/logout"}


@app.middleware("http")
async def access_boundary(request: Request, call_next):
    try:
        target = urlsplit("//" + request.headers.get("host", ""))
        host = target.hostname
        remote = bool(config.REMOTE_HOST and host == config.REMOTE_HOST)
        if not remote and host not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Unknown host"}, status_code=400)
        if remote and target.port not in {None, 443}:
            return JSONResponse({"detail": "Use the configured HTTPS host"}, status_code=400)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin or config.REMOTE_HOST:
                source = urlsplit(origin or "")
                default_port = 443 if remote else 80
                if (source.scheme != ("https" if remote else "http") or source.hostname != host
                        or (source.port or default_port) != (target.port or default_port)):
                    return JSONResponse({"detail": "Cross-origin writes are blocked"}, status_code=403)
    except ValueError:
        return JSONResponse({"detail": "Invalid origin or host"}, status_code=400)
    request.state.remote = remote
    request.state.actor = None
    path = request.url.path
    if remote and path in {"/docs", "/redoc", "/docs/oauth2-redirect", "/openapi.json", "/health"}:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if path.startswith("/api/"):
        if config.REMOTE_HOST:
            request.state.actor = await run_in_threadpool(access.read_session, request.cookies.get(access.COOKIE))
        else:
            request.state.actor = {"id": "local", "name": "Local browser", "role": "owner", "status": "approved"}
        actor = request.state.actor
        if path == "/api/access/enroll" and not access.rate_allowed("enrollment", 20):
            return JSONResponse({"detail": "Too many pairing attempts. Try again in a minute."}, status_code=429)
        if path not in PUBLIC_ACCESS and (not actor or actor["status"] != "approved"):
            return JSONResponse({"detail": "This device is not approved."}, status_code=401)
        if actor and request.method == "POST" and not access.rate_allowed(actor["id"], 90):
            return JSONResponse({"detail": "Request limit reached. Try again in a minute."}, status_code=429)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                   "img-src 'self' data:; media-src 'self' blob:; worker-src 'self'; "
                                                   "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

WEB_ROOT = Path(__file__).parent / "web"

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(default="web", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

class ApprovalIn(BaseModel):
    approved: bool


class ChatStreamResponse(StreamingResponse):
    async def stream_response(self, send):
        try:
            await super().stream_response(send)
        finally:
            # A disconnect can happen while sending a chunk, with the generator still suspended.
            with CancelScope(shield=True):
                await self.body_iterator.aclose()

@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_ROOT / "index.html").read_text(encoding="utf-8")

@app.get("/dashboard.js", include_in_schema=False)
def dashboard_script():
    return FileResponse(WEB_ROOT / "dashboard.js", media_type="text/javascript")

@app.get("/health")
def health():
    return {"status": "ok", "model": config.MODEL, "embeddings": config.EMBED_MODEL or "keyword-only"}

def begin_run(inp: ChatIn, request: Request | None):
    if request is None:
        return None, "local", None
    actor = access.approved(request)["id"]
    key = request.headers.get("Idempotency-Key", "")
    if not key and not config.REMOTE_HOST:
        return None, actor, None
    if key and not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
        raise HTTPException(400, "Invalid idempotency key.")
    try:
        run, previous = activity.begin(actor, inp.session_id, inp.message, key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if previous and not previous["result"]:
        raise HTTPException(409, f"That request is already recorded as {previous['state']}.")
    return run, actor, previous


@app.post("/api/chat")
def chat(inp: ChatIn, request: Request):
    run, actor, previous = begin_run(inp, request)
    if previous:
        return {k: v for k, v in previous["result"].items() if k != "type"}
    try:
        return agent.ask(inp.message, inp.session_id, actor=actor, run_id=run)
    except (OllamaError, ValueError) as exc:
        activity.transition(run, "FAILED")
        raise HTTPException(status_code=503 if isinstance(exc, OllamaError) else 400, detail=str(exc)) from exc


@app.post("/api/chat/stream")
def chat_stream(inp: ChatIn, request: Request = None):
    run, actor, previous = begin_run(inp, request)

    async def events():
        if previous:
            yield f"data: {json.dumps(previous['result'], ensure_ascii=False)}\n\n"
            return
        stream = agent.ask_stream(inp.message, inp.session_id, actor=actor, run_id=run)
        finished = False
        try:
            while (event := await to_thread.run_sync(next, stream, None)) is not None:
                finished = event["type"] == "done"
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except (OllamaError, ValueError) as exc:
            await to_thread.run_sync(activity.transition, run, "FAILED")
            finished = True
            status = "503" if isinstance(exc, OllamaError) else "400"
            yield f"data: {json.dumps({'type':'error','status':status,'detail':str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            with CancelScope(shield=True):
                await to_thread.run_sync(stream.close)
                if not finished:
                    await to_thread.run_sync(activity.transition, run, "CANCELLED")

    return ChatStreamResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/activity")
def get_activity():
    return activity.snapshot()


def live_snapshot() -> dict:
    config.validate_ollama_url()
    try:
        with http.urlopen(config.OLLAMA_URL + "/api/tags", timeout=1.5) as response:
            ollama = config.MODEL in {m["name"] for m in json.loads(response.read(512 * 1024)).get("models", [])}
    except (ValueError, KeyError, OSError):
        ollama = False
    return {"laptop": desktop.laptop_status(), "model": config.MODEL, "ollama": ollama,
            "activity": activity.snapshot(), "automations": automation.snapshot()}


@app.get("/api/live")
def live(request: Request):
    async def events():
        while True:
            if config.REMOTE_HOST:
                actor = await run_in_threadpool(access.read_session, request.cookies.get(access.COOKIE))
                if not actor or actor["status"] != "approved":
                    yield "event: locked\ndata: {}\n\n"
                    break
            snapshot = await run_in_threadpool(live_snapshot)
            yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            await asyncio.sleep(8)
    return ChatStreamResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


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
    result = storage.pending_approvals(session_id)
    for item in result:
        try:
            preview = tools.approval_preview(item["tool_name"], item["arguments"])
        except (ValueError, KeyError):
            preview = None
        if preview:
            item["preview"] = preview
    return result

@app.post("/api/approvals/{token}")
def approve(token: str, body: ApprovalIn, request: Request):
    result = agent.decide_approval(token, body.approved, access.approved(request)["id"])
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
        return await run_in_threadpool(rag.ingest_bytes, name, contents)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


WEB_ASSETS = {"control.js": "text/javascript", "manifest.webmanifest": "application/manifest+json",
              "sw.js": "text/javascript", "icon-192.png": "image/png", "icon-512.png": "image/png"}


@app.get("/{file_name}", include_in_schema=False)
def frontend_asset(file_name: str):
    if file_name not in WEB_ASSETS:
        raise HTTPException(404, "Not found")
    return FileResponse(WEB_ROOT / file_name, media_type=WEB_ASSETS[file_name])


