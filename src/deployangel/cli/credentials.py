"""Tokens `deployangel setup` saved for a project, so they never have to be
printed, pasted into a coding agent's chat, or kept in the environment:
.deployangel/credentials.json, readable only by its owner. The directory
carries its own .gitignore, so it stays out of git without touching the
project's. Same file and layout as the Ruby gem's."""

from __future__ import annotations

import json
import os
from typing import Optional

DIR = ".deployangel"
FILE = "credentials.json"


def path(root: str) -> str:
    return os.path.join(root, DIR, FILE)


def find(root: str) -> Optional[str]:
    """From root or a parent directory, as git finds .git."""
    directory = os.path.abspath(root)
    while True:
        candidate = path(directory)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def read(root: str) -> dict:
    file = find(root)
    if not file:
        return {}
    try:
        with open(file) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write(root: str, data: dict) -> str:
    directory = os.path.join(root, DIR)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, ".gitignore"), "w") as ignore:
        ignore.write("*\n")
    file = path(root)
    descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, indent=2)
    os.chmod(file, 0o600)
    return file
