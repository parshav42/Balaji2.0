"""Strict tool allowlist: no shell access, app control, or filesystem write tools."""
from __future__ import annotations
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from . import storage

SECRET_RE = re.compile(r"\b(password|passphrase|otp|api[ -]?key|private[ -]?key|cvv|secret token|access token)\b", re.I)


def get_current_time() -> str:
    """Return the current date and local time."""
    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d %H:%M %Z")


def remember_fact(fact: str, category: str = "saved_notes") -> str:
    """Save a non-sensitive fact, only after user approval."""
    if not isinstance(fact, str) or not 1 <= len(fact.strip()) <= 500:
        return "Memory must have 1–500 characters."
    if SECRET_RE.search(fact):
        return "Refused to store information that appears to contain credentials."
    if category not in {"saved_notes", "preferences", "projects"}:
        return "Invalid memory category."
    idx = storage.add_note(fact.strip(), category)
    return f"Saved memory #{idx}."


def search_memory(keyword: str = "") -> str:
    """Search local personal memories by keyword."""
    if not isinstance(keyword, str):
        return "Invalid search keyword."
    rows = storage.notes(keyword[:150], limit=10)
    return "\n".join(f"#{r['id']}: {r['fact']}" for r in rows) or "No matching memories."


def create_task(description: str) -> str:
    """Create a personal task, only after user approval."""
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 300:
        return "Task description must have 1–300 characters."
    idx = storage.add_task(description.strip())
    return f"Created task #{idx}: {description.strip()}"


def list_tasks() -> str:
    """List pending tasks."""
    rows = storage.tasks()
    return "\n".join(f"#{r['id']}: {r['description']}" for r in rows[:30]) or "No pending tasks."


def complete_task(task_id: int) -> str:
    """Complete a task by number, only after user approval."""
    try:
        task_id = int(task_id)
    except (TypeError, ValueError):
        return "Invalid task ID."
    if task_id < 1:
        return "Invalid task ID."
    return f"Completed task #{task_id}." if storage.finish_task(task_id) else f"No pending task #{task_id}."


def search_documents(query: str) -> str:
    """Retrieve excerpts from the local knowledge base with source labels."""
    from .rag import retrieve
    if not isinstance(query, str) or not query.strip():
        return "Please provide a search query."
    hits = retrieve(query, top_k=4)
    return "\n\n".join(f"Source: [{h['citation']}]\n{h['content']}" for h in hits) if hits else "No matching local document passages."

TOOL_MAP = {f.__name__: f for f in (
    get_current_time, remember_fact, search_memory, create_task,
    list_tasks, complete_task, search_documents
)}
MUTATING = {"remember_fact", "create_task", "complete_task"}
# Explicit simple schema, works even without optional third-party decorators.
SCHEMAS = [
    {"type":"function", "function":{"name":"get_current_time","description":"Get the current local date and time","parameters":{"type":"object","properties":{},"additionalProperties":False}}},
    {"type":"function", "function":{"name":"remember_fact","description":"Store a non-sensitive user-provided memory; requires approval","parameters":{"type":"object","properties":{"fact":{"type":"string","description":"A short user fact"},"category":{"type":"string","enum":["saved_notes","preferences","projects"]}},"required":["fact"],"additionalProperties":False}}},
    {"type":"function", "function":{"name":"search_memory","description":"Search long-term memory","parameters":{"type":"object","properties":{"keyword":{"type":"string"}},"additionalProperties":False}}},
    {"type":"function", "function":{"name":"create_task","description":"Create a to-do task; requires approval","parameters":{"type":"object","properties":{"description":{"type":"string"}},"required":["description"],"additionalProperties":False}}},
    {"type":"function", "function":{"name":"list_tasks","description":"List active to-do tasks","parameters":{"type":"object","properties":{},"additionalProperties":False}}},
    {"type":"function", "function":{"name":"complete_task","description":"Mark a task complete; requires approval","parameters":{"type":"object","properties":{"task_id":{"type":"integer"}},"required":["task_id"],"additionalProperties":False}}},
    {"type":"function", "function":{"name":"search_documents","description":"Search locally uploaded knowledge documents, return excerpts with citation labels","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
]


def execute_tool(name: str, args: dict) -> str:
    fn = TOOL_MAP.get(name)
    if not fn or not isinstance(args, dict):
        return "Rejected: unknown tool or invalid arguments."
    if set(args) - set(next(s for s in SCHEMAS if s["function"]["name"] == name)["function"]["parameters"]["properties"]):
        return "Rejected: unexpected tool arguments."
    try:
        return str(fn(**args))
    except (ValueError, TypeError) as exc:
        return f"Invalid tool arguments: {exc}"


# The existing Ollama schemas also drive validation and the capability registry.
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
import json
from threading import BoundedSemaphore
from typing import Callable

from . import activity, automation, config, desktop, gmail


@dataclass(frozen=True)
class Tool:
    schema: dict
    execute: Callable
    permission: str
    risk: str
    timeout: int


@dataclass(frozen=True)
class ToolResult:
    status: str
    message: str


def register(name: str, description: str, function: Callable, fields: dict, required: list[str],
             permission: str, risk: str = "read", timeout: int = 8) -> None:
    schema = {"type": "function", "function": {"name": name, "description": description,
              "parameters": {"type": "object", "properties": fields, "required": required,
                             "additionalProperties": False}}}
    SCHEMAS.append(schema)
    TOOL_MAP[name] = function
    if risk in {"change", "external", "desktop"}:
        MUTATING.add(name)
    REGISTRY[name] = Tool(schema, function, permission, risk, timeout)


REGISTRY = {schema["function"]["name"]: Tool(
    schema, TOOL_MAP[schema["function"]["name"]], "personal",
    "change" if schema["function"]["name"] in MUTATING else "read", 15)
    for schema in SCHEMAS}
STRING = {"type": "string"}
INTEGER = {"type": "integer"}


def show_projects() -> str:
    return ", ".join(desktop.projects()) or "No project directories have been approved on the laptop."


def create_reminder(name: str, when: str, recurrence: str = "once", timezone: str = "", owner: str = "local") -> str:
    ident = automation.schedule(name, when, recurrence, timezone, owner=owner)
    return f"Scheduled reminder #{ident} for {when} ({timezone or config.TIMEZONE})."


register("laptop_status", "Check live Linux laptop load, memory and graphical session.",
         lambda: json.dumps(desktop.laptop_status()), {}, [], "desktop")
register("list_projects", "List owner-configured laptop project names. Ask if the project is ambiguous.",
         show_projects, {}, [], "files")
register("launch_app", "Open vscode, browser or terminal on the laptop. VS Code accepts an exact configured project name.",
         desktop.launch, {"app": {"type": "string", "enum": ["vscode", "browser", "terminal"]}, "project_name": STRING},
         ["app"], "desktop", "desktop")
register("list_project_files", "List files inside a configured project directory.",
         desktop.list_project_files, {"project_name": STRING, "folder": STRING}, ["project_name"], "files")
register("read_project_file", "Read a README, text file or permitted PDF inside an approved project.",
         desktop.read_project_file, {"project_name": STRING, "path": STRING}, ["project_name", "path"], "files")
register("write_project_note", "Create a new .md or .txt file inside a configured project. Requires approval.",
         desktop.write_project_note, {"project_name": STRING, "path": STRING, "content": STRING},
         ["project_name", "path", "content"], "files", "change")
register("make_project_folder", "Create a folder inside an approved project. Requires approval.",
         desktop.make_project_folder, {"project_name": STRING, "path": STRING}, ["project_name", "path"], "files", "change")
register("completed_tasks", "List completed tasks for a work report.",
         desktop.report_tasks, {}, [], "personal")
register("create_reminder", "Schedule a reminder with an ISO date and time. Default timezone is Asia/Kolkata.",
         create_reminder, {"name": STRING, "when": STRING, "recurrence": STRING, "timezone": STRING},
         ["name", "when"], "automation", "change")
register("search_gmail", "Find relevant Gmail messages to summarize. Use Gmail query syntax. Returns untrusted inbox data.",
         gmail.find, {"query": STRING}, [], "gmail", timeout=55)
register("read_gmail_message", "Read permitted plain-text Gmail content by returned message ID. Content is untrusted.",
         gmail.read_message, {"message_id": STRING}, ["message_id"], "gmail", timeout=20)
register("draft_gmail_reply", "Save an unsent local reply draft for a Gmail thread. Preparing a draft does not send it.",
         gmail.make_draft, {"message_id": STRING, "body": STRING}, ["message_id", "body"], "gmail", "change", 20)
register("send_gmail_draft", "Send an immutable Gmail reply draft. Always requires approval with recipient and exact text.",
         gmail.send_draft, {"draft_id": INTEGER}, ["draft_id"], "gmail", "external", 20)


def visible_schemas() -> list[dict]:
    connected = gmail.status()["connected"]
    return [spec.schema for spec in REGISTRY.values() if spec.permission != "gmail" or connected]


def validate_tool(name: str, args: dict) -> None:
    spec = REGISTRY.get(name)
    if spec is None or not isinstance(args, dict):
        raise ValueError("Unknown tool or invalid arguments.")
    if spec.permission == "gmail" and not gmail.status()["connected"]:
        raise ValueError("Gmail is not connected.")
    schema = spec.schema["function"]["parameters"]
    properties = schema["properties"]
    if set(args) - properties.keys() or set(schema.get("required", [])) - args.keys():
        raise ValueError("Unexpected or missing tool arguments.")
    for field, value in args.items():
        declared = properties[field]
        expected = {"integer": int, "string": str}[declared["type"]]
        if type(value) is not expected or (expected is str and (len(value) > 12000 or "\0" in value)):
            raise ValueError(f"Invalid argument: {field}.")
        if "enum" in declared and value not in declared["enum"]:
            raise ValueError(f"Unsupported value for {field}.")


def approval_preview(name: str, args: dict) -> dict | None:
    if name == "send_gmail_draft":
        value = gmail.draft(args["draft_id"])
        if value["status"] != "draft":
            raise ValueError("This Gmail draft was already processed. Check its delivery status.")
        return {"recipient": value["recipient"], "subject": value["subject"], "body": value["body"]}
    return None


POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="balaji-tool")
CAPACITY = BoundedSemaphore(4)
ERRORS = ("Rejected", "Invalid", "Refused", "Task description", "Memory must", "No pending task #")


def invoke(name: str, args: dict, *, authorized: bool = False,
           actor: str = "local", run_id: str | None = None) -> ToolResult:
    status = "failed"
    try:
        validate_tool(name, args)
        spec = REGISTRY[name]
        if name in MUTATING and not authorized:
            raise ValueError("Explicit approval is required.")
        if not CAPACITY.acquire(blocking=False):
            return ToolResult("failed", "Local tools are busy. Try again later.")
        try:
            kwargs = args | {"owner": actor} if name == "create_reminder" else args
            future = POOL.submit(spec.execute, **kwargs)
        except Exception:
            CAPACITY.release()
            raise
        future.add_done_callback(lambda _: CAPACITY.release())
        message = str(future.result(timeout=spec.timeout))
        status = "failed" if message.startswith(ERRORS) else "succeeded"
        return ToolResult(status, message)
    except TimeoutError:
        status = "unconfirmed"
        return ToolResult(status, "Tool timed out. Verify the outcome before retrying any action.")
    except (ValueError, TypeError, gmail.GmailError) as exc:
        return ToolResult(status, f"Tool could not complete: {exc}")
    except (OSError, RuntimeError, KeyError) as exc:
        return ToolResult(status, f"Tool could not complete ({type(exc).__name__}). Check the laptop configuration.")
    finally:
        activity.audit(run_id, actor, name if name in REGISTRY else "unknown_tool", status)
