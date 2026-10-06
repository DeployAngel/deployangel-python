"""What the application contains, sent once per process: the route table, job
classes, declared recurring schedules, critical flows, and file digests.
Paths and hashes only; source code never leaves the application.

Routes, jobs, and schedules come from the integrations in use (sources), each
reporting what it knows. A process may know only part of the app (a FastAPI
web process doesn't load the Celery tasks its worker runs), so the cloud
merges what every process of a release reports."""

from __future__ import annotations

import hashlib
import os
from typing import Optional

MAX_FILES = 20_000
DIGEST_LENGTH = 16
# Code, templates, and the files that pin dependencies and configuration.
DIGEST_EXTENSIONS = (".py", ".html", ".htm", ".jinja", ".jinja2", ".j2", ".txt", ".toml", ".cfg", ".ini",
                     ".yml", ".yaml", ".lock", ".json", ".sql")
DIGEST_NAMES = ("Pipfile", "Procfile", "Dockerfile")
# Directories that aren't the app's own source: virtualenvs (on Heroku,
# .heroku/python), installed and built assets, caches, and VCS data.
SKIPPED_DIRECTORIES = {".git", ".hg", ".heroku", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox",
                       ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", "staticfiles", "static_root",
                       "media", "tmp", "log", "logs", "dist", "build", ".profile.d", "htmlcov"}


class Metadata:
    def __init__(self, config, root: str, environment: str, sources: Optional[list] = None):
        self.config = config
        self.root = root
        self.environment = environment
        self.sources = sources if sources is not None else []
        self._manifest: Optional[dict] = None

    def to_protocol(self) -> dict:
        manifest = self.file_manifest()
        return {
            "routes": self.routes(),
            "job_classes": sorted(set(self._collect("job_classes"))),
            "job_class_files": self.job_class_files(),
            "schedules": self._collect("schedules"),
            "critical_flows": {str(name): [str(item) for item in (items if isinstance(items, (list, tuple)) else [items])]
                               for name, items in (self.config.critical_flows or {}).items()},
            "file_manifest": {key: manifest[key] for key in ("hash", "count", "truncated")},
        }

    def files(self) -> dict:
        return self.file_manifest()["files"]

    def routes(self) -> list:
        seen = set()
        routes = []
        for route in self._collect("routes"):
            key = route.get("key")
            if key and key not in seen and not self.config.is_ignored_route(key):
                seen.add(key)
                routes.append(route)
        return routes

    def job_class_files(self) -> dict:
        files: dict = {}
        for source in self.sources:
            method = getattr(source, "job_class_files", None)
            if method is None:
                continue
            try:
                for name, paths in (method() or {}).items():
                    files.setdefault(name, []).extend(path for path in paths if path not in files.get(name, []))
            except Exception:
                continue
        return files

    def _collect(self, name: str) -> list:
        """Each source is read on its own, so one that fails leaves the others'
        answers in place."""
        items: list = []
        for source in self.sources:
            method = getattr(source, name, None)
            if method is None:
                continue
            try:
                items.extend(method() or [])
            except Exception:
                continue
        return items

    def file_manifest(self) -> dict:
        if self._manifest is None:
            self._manifest = self._build_manifest()
        return self._manifest

    def _build_manifest(self) -> dict:
        empty = {"hash": None, "count": 0, "truncated": False, "files": {}}
        if not self.config.file_digests or not self.root:
            return empty
        try:
            paths = sorted(self._digest_paths())
            truncated = len(paths) > MAX_FILES
            files = {}
            for relative in paths[:MAX_FILES]:
                digest = _file_digest(os.path.join(self.root, relative))
                if digest is not None:
                    files[relative] = digest
            joined = "\n".join(f"{path}:{digest}" for path, digest in files.items())
            return {"hash": hashlib.sha256(joined.encode()).hexdigest(), "count": len(files), "truncated": truncated,
                    "files": files}
        except Exception:
            return empty

    def _digest_paths(self) -> list:
        """Relative paths, with forward slashes, of the files digested to tell
        which routes a release changed."""
        paths = []
        for directory, subdirectories, filenames in os.walk(self.root):
            subdirectories[:] = [name for name in subdirectories
                                 if name not in SKIPPED_DIRECTORIES and not name.endswith(".egg-info")
                                 and not os.path.exists(os.path.join(directory, name, "pyvenv.cfg"))]
            for filename in filenames:
                if filename.endswith(DIGEST_EXTENSIONS) or filename in DIGEST_NAMES:
                    path = os.path.relpath(os.path.join(directory, filename), self.root).replace(os.sep, "/")
                    paths.append(path)
            if len(paths) > MAX_FILES * 2:
                break
        return paths


def _file_digest(path: str) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as file:
            for chunk in iter(lambda: file.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()[:DIGEST_LENGTH]
    except OSError:
        return None


def module_file(module_name: Optional[str], root: Optional[str]) -> Optional[str]:
    """The app-relative path of a module's source file, or None for a module
    outside the app (an installed package)."""
    import sys

    from deployangel.core.fingerprint import is_app_path, normalize_path

    module = sys.modules.get(module_name or "")
    path = getattr(module, "__file__", None)
    if not path or not root:
        return None
    path = os.path.abspath(path)
    if path.endswith(".pyc"):
        path = path[:-1]
    return normalize_path(path, root) if is_app_path(path, root) else None
