"""Desktop tools constrained to owner-configured project roots and fixed executables."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from . import config, storage

APPS = {
    "vscode": ("/usr/bin/code", "/usr/share/code/bin/code"),
    "browser": ("/usr/bin/firefox", "/usr/bin/brave-browser", "/usr/bin/chromium"),
    "terminal": ("/usr/bin/x-terminal-emulator",),
}
READABLE = {".txt", ".md", ".pdf"}
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_LIMIT = 512 * 1024


def laptop_status() -> dict:
    data = {"platform": "Linux", "session": os.environ.get("XDG_SESSION_TYPE", "unknown"),
            "desktop_available": bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
            "load_1m": round(os.getloadavg()[0], 2), "logical_cpus": os.cpu_count()}
    mem = {}
    with open("/proc/meminfo", encoding="ascii") as source:
        for line in source:
            key, _, value = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                mem[key] = int(value.split()[0]) // 1024
    return data | {"ram_total_mib": mem["MemTotal"], "ram_used_mib": mem["MemTotal"] - mem["MemAvailable"]}


def projects() -> dict[str, Path]:
    if not config.PROJECTS_FILE:
        return {}
    with open(Path(config.PROJECTS_FILE).expanduser(), encoding="utf-8") as source:
        configured = json.load(source)
    if not isinstance(configured, dict) or len(configured) > 50:
        raise ValueError("Project configuration must map up to 50 names to absolute paths.")
    result = {}
    for label, value in configured.items():
        if not re.fullmatch(r"[\w -]{1,60}", label) or not isinstance(value, str):
            raise ValueError("Invalid project mapping.")
        path = Path(value).expanduser()
        if not path.is_absolute() or path == Path("/") or path == Path.home() or path.resolve() != path:
            raise ValueError("Use a specific, canonical project directory, away from home or filesystem root.")
        result[label] = path
    return result


def project(label: str) -> Path:
    configured = projects()
    matches = [root for name, root in configured.items() if name.casefold() == label.strip().casefold()]
    if len(matches) != 1:
        raise ValueError("Choose an exact configured project name. Available projects: " + (", ".join(configured) or "none"))
    return matches[0]


def path_parts(value: str) -> list[str]:
    parts = value.split("/") if value else []
    if len(parts) > 12 or any(not re.fullmatch(r"[\w -][\w .-]{0,119}", part) for part in parts):
        raise ValueError("Use a relative path within the selected project. Hidden and parent paths are blocked.")
    return parts


@contextmanager
def directory(root: Path, parts: list[str]):
    # Walk from the filesystem root without following any symlink, including parents of the allowlisted root.
    fd = os.open("/", DIRECTORY)
    try:
        for part in [*root.parts[1:], *parts]:
            next_fd = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def list_project_files(project_name: str, folder: str = "") -> str:
    with directory(project(project_name), path_parts(folder)) as fd, os.scandir(fd) as entries:
        names = []
        for entry in entries:
            if not entry.name.startswith(".") and not entry.is_symlink():
                names.append(entry.name + ("/" if entry.is_dir(follow_symlinks=False) else ""))
                if len(names) >= 80:
                    break
    return "Project entries (up to 80): " + (", ".join(sorted(names)) or "none")


def read_project_file(project_name: str, path: str) -> str:
    parts = path_parts(path)
    extension = Path(path).suffix.lower()
    if not parts or extension not in READABLE:
        raise ValueError("Select an approved .txt, .md or .pdf project file.")
    with directory(project(project_name), parts[:-1]) as parent:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > FILE_LIMIT:
                raise ValueError("Only regular files under 512 KiB with one link are supported.")
            if extension == ".pdf":
                from pypdf import PdfReader
                pdf = PdfReader(source)
                text = "\n".join((page.extract_text() or "")[:1500] for page in pdf.pages[:5])
            else:
                text = source.read(FILE_LIMIT).decode("utf-8", errors="replace")[:7500]
    return f"Untrusted reference content from [{project_name}/{path}]:\n{text[:7500]}"


def write_project_note(project_name: str, path: str, content: str) -> str:
    parts = path_parts(path)
    if not parts or Path(path).suffix.lower() not in {".md", ".txt"} or not content.strip() or len(content) > 12000:
        raise ValueError("Provide a new Markdown or text file and 1–12000 characters.")
    data = content.encode()
    with directory(project(project_name), parts[:-1]) as parent:
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
        except OSError:
            os.unlink(parts[-1], dir_fd=parent)
            raise
    return f"Saved {len(data)} bytes to {project_name}/{path}."


def make_project_folder(project_name: str, path: str) -> str:
    parts = path_parts(path)
    if not parts:
        raise ValueError("Provide a new folder name within the configured project.")
    with directory(project(project_name), parts[:-1]) as parent:
        os.mkdir(parts[-1], mode=0o700, dir_fd=parent)
    return f"Created folder {project_name}/{path}."


def launch(app: str, project_name: str = "") -> str:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise ValueError("No Linux graphical session is available to the BALAJI process.")
    choices = APPS.get(app)
    if not choices:
        raise ValueError("Choose vscode, browser, or terminal.")
    exe = next((path for path in choices if os.path.isfile(path) and os.access(path, os.X_OK)), None)
    if not exe:
        raise ValueError(f"{app} is unavailable on this laptop.")
    if project_name and app != "vscode":
        raise ValueError("Only VS Code accepts a project argument.")
    root = project(project_name) if project_name else None
    if root:
        # Verify the allowlisted path exists and has no symlink components before handoff to VS Code.
        with directory(root, []):
            pass
    argv = [exe, "--reuse-window", str(root)] if root else [exe]
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        status = process.wait(timeout=0.8)
    except subprocess.TimeoutExpired:
        from threading import Thread
        Thread(target=process.wait, daemon=True).start()
        return f"Started {app} (PID {process.pid}); graphical window confirmation is unavailable."
    if status != 0:
        raise ValueError(f"{app} launch returned exit code {status}.")
    return f"{app} accepted the launch request" + (f" for {project_name}." if root else ".")


def report_tasks() -> str:
    rows = [r["description"] for r in storage.tasks(include_done=True) if r["completed"]]
    return "\n".join(f"- {description}" for description in rows[:40]) or "No completed tasks are available."
