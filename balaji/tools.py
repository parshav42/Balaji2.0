"""Strict tool allowlist: no shell access, app control, or filesystem write tools."""
from __future__ import annotations
import re
from datetime import datetime
from . import storage

SECRET_RE = re.compile(r"\b(password|passphrase|otp|api[ -]?key|private[ -]?key|cvv|secret token|access token)\b", re.I)


def get_current_time() -> str:
    """Return the current date and local time."""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")


def remember_fact(fact: str) -> str:
    """Save a non-sensitive fact, only after user approval."""
    if not isinstance(fact, str) or not 1 <= len(fact.strip()) <= 500:
        return "Memory must have 1–500 characters."
    if SECRET_RE.search(fact):
        return "Refused to store information that appears to contain credentials."
    idx = storage.add_note(fact.strip())
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
    {"type":"function", "function":{"name":"remember_fact","description":"Store a non-sensitive user-provided memory; requires approval","parameters":{"type":"object","properties":{"fact":{"type":"string","description":"A short user fact"}},"required":["fact"],"additionalProperties":False}}},
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
