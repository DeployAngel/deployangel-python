"""Exception fingerprint algorithm v1. Stable across deployments: no line
numbers, no messages, no package versions, no absolute paths, and no
Python-version-specific function naming (co_name, never co_qualname)."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Iterable, Optional

VERSION = 1
MESSAGE_PLACEHOLDERS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", re.ASCII), "<email>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.ASCII | re.IGNORECASE), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{16,}\b", re.ASCII | re.IGNORECASE), "<hex>"),
    (re.compile(r"([\"'`]).*?\1"), "<string>"),
    (re.compile(r"\b\d+(\.\d+)?\b", re.ASCII), "<n>"),
]
MAX_MESSAGE = 200
MAX_BACKTRACE = 20
MAX_FRAMES = 200
# Installed packages, wherever the virtualenv lives (on Heroku it's under the
# app's root, in .heroku/python).
PACKAGES = re.compile(r"[/\\](?:site|dist)-packages[/\\](.+)\Z")
STDLIB = re.compile(r"[/\\]lib[/\\]python\d+\.\d+[/\\](.+)\Z")
NOT_APP_DIRS = (".heroku", ".venv", "venv", "env", "node_modules", "tmp", ".tox")


def for_exception(exception: BaseException, root: Optional[str], message: bool = True, redactions: Iterable = ()) -> dict:
    """message=False leaves the message out entirely. redactions are
    (value, placeholder) pairs replaced first (Redaction)."""
    return details(exception_class(exception), locations(exception), root,
                   message=_safe_str(exception) if message else None, redactions=redactions)


def details(class_name: str, frames: list, root: Optional[str], message: Optional[str] = None, redactions: Iterable = ()) -> dict:
    frame, app = top_frame(frames, root)
    digest = hashlib.sha256("|".join([f"v{VERSION}", class_name, frame]).encode()).hexdigest()[:32]
    return {
        "fingerprint": digest,
        "fingerprint_version": VERSION,
        "exception_class": class_name,
        "message": normalize_message(message, redactions) if message is not None else None,
        "top_frame": frame,
        "app_frame": app,
    }


def exception_class(exception: BaseException) -> str:
    cls = type(exception)
    module = getattr(cls, "__module__", None)
    return cls.__qualname__ if module in (None, "builtins", "__main__") else f"{module}.{cls.__qualname__}"


def locations(exception: BaseException) -> list:
    """(path, function) pairs, innermost first, read from the traceback's code
    objects without touching source files."""
    frames = []
    tb = exception.__traceback__
    while tb is not None and len(frames) < MAX_FRAMES:
        code = tb.tb_frame.f_code
        frames.append((code.co_filename, code.co_name))
        tb = tb.tb_next
    frames.reverse()
    return frames


def top_frame(frames: list, root: Optional[str]) -> tuple:
    """First application frame as "relative/path.py#function", else the first
    frame normalized as "package/module.py#function"; and whether it's the
    application's."""
    for path, name in frames:
        if is_app_path(path, root):
            return f"{normalize_path(path, root)}#{name}", True
    if not frames:
        return "unknown", False
    path, name = frames[0]
    return f"{normalize_path(path, root)}#{name}", False


def backtrace(exception: BaseException, root: Optional[str]) -> list:
    return app_backtrace(locations(exception), root)


def app_backtrace(frames: list, root: Optional[str]) -> list:
    """Application frames only, relative to the root."""
    return [f"{normalize_path(path, root)}#{name}" for path, name in frames if is_app_path(path, root)][:MAX_BACKTRACE]


def normalize_message(message, redactions: Iterable = ()) -> str:
    """Only quoted values, numbers, emails, UUIDs, and long hex are replaced;
    unquoted words, such as a name in a message the app builds, are kept."""
    lines = str(message).splitlines()
    text = lines[0].strip() if lines else ""
    for value, placeholder in redactions:
        text = re.sub(r"(?<![^\W_])" + re.escape(value) + r"(?![^\W_])", placeholder, text, flags=re.IGNORECASE)
    for pattern, placeholder in MESSAGE_PLACEHOLDERS:
        text = pattern.sub(placeholder, text)
    return text[:MAX_MESSAGE]


def is_app_path(path: Optional[str], root: Optional[str]) -> bool:
    relative = _relative(path, root)
    if relative is None or PACKAGES.search(path):
        return False
    return relative.split("/", 1)[0] not in NOT_APP_DIRS


def normalize_path(path: str, root: Optional[str]) -> str:
    if is_app_path(path, root):
        return _relative(path, root)
    match = PACKAGES.search(path) or STDLIB.search(path)
    if match:
        return match.group(1).replace("\\", "/")
    return os.path.basename(path) if path.startswith(("/", "\\")) or ":" in path[:3] else path


def _relative(path: Optional[str], root: Optional[str]) -> Optional[str]:
    """The path under root, with forward slashes, or None."""
    if not root or not path:
        return None
    root = root.rstrip("/\\")
    if not path.startswith(root) or path[len(root):len(root) + 1] not in ("/", "\\") or len(path) <= len(root) + 1:
        return None
    return path[len(root) + 1:].replace("\\", "/")


def _safe_str(exception: BaseException) -> str:
    try:
        return str(exception)
    except Exception:
        return ""


# Parsing a formatted traceback, for exceptions only known as text (an RQ job
# that ran in a forked work horse). Only the last exception of a chain counts,
# as it does for the exception object.
_FRAME = re.compile(r'^\s*File "(.+)", line \d+, in (.+?)\s*$')
_EXCEPTION_LINE = re.compile(r"^([A-Za-z_][\w.]*)(?::\s?(.*))?$")


def parse_traceback(text: str) -> Optional[tuple]:
    """(class name, frames innermost first, message), or None."""
    if not text:
        return None
    block = text.rsplit("Traceback (most recent call last):", 1)[-1]
    frames = []
    class_name = None
    message = None
    for line in block.splitlines():
        frame = _FRAME.match(line)
        if frame:
            frames.append((frame.group(1), frame.group(2)))
        elif line and not line[0].isspace() and class_name is None and frames:
            match = _EXCEPTION_LINE.match(line.rstrip())
            if match:
                class_name, message = match.group(1), match.group(2) or ""
    if class_name is None:
        return None
    frames.reverse()
    return class_name, frames, message
