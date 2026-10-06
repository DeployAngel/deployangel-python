"""Which release this process is running, resolved once at boot so that
telemetry can be attributed to a deployment."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Callable, Mapping, Optional

COMMIT_FORMAT = re.compile(r"\A[0-9a-f]{7,40}\Z")
# Tags that move from build to build, so they can't identify a release.
MOVING_TAGS = ("latest", "main", "master", "production", "prod", "staging", "stable", "release")


class Release:
    __slots__ = ("version", "commit", "source")

    def __init__(self, version: Optional[str], commit: Optional[str], source: str):
        self.version = version
        self.commit = commit
        self.source = source

    @property
    def unknown(self) -> bool:
        return self.version is None and self.commit is None

    def to_protocol(self) -> dict:
        return {"version": self.version, "commit": self.commit, "source": self.source}

    def __repr__(self) -> str:
        return f"Release(version={self.version!r}, commit={self.commit!r}, source={self.source!r})"


def resolve(config, env: Mapping[str, str] = os.environ, root: Optional[str] = None,
            http: Optional[Callable[[str], Optional[str]]] = None) -> Release:
    """Order: explicit configuration, Heroku dyno metadata, the hosting
    platform's own variables (Kamal, Render, Fly.io, Railway, Coolify, and
    Dokku's GIT_REV), a REVISION file, then ECS container metadata."""
    http = http or fetch_metadata
    if _present(config.release_version) or _present(config.revision):
        return build(config.release_version, config.revision, "config")
    if _present(env.get("HEROKU_RELEASE_VERSION")) or _present(heroku_commit(env)):
        return build(env.get("HEROKU_RELEASE_VERSION"), heroku_commit(env), "heroku_dyno_metadata")
    if _present(env.get("KAMAL_VERSION")):
        return kamal(env["KAMAL_VERSION"])
    if _present(env.get("RENDER_GIT_COMMIT")):
        return build(None, env["RENDER_GIT_COMMIT"], "render")
    tag = fly_tag(env.get("FLY_IMAGE_REF"))
    if tag:
        return build(tag, None, "fly")
    if _present(env.get("RAILWAY_GIT_COMMIT_SHA")) or _present(env.get("RAILWAY_DEPLOYMENT_ID")):
        return railway(env)
    if _present(env.get("SOURCE_COMMIT")) and _present(env.get("COOLIFY_CONTAINER_NAME") or env.get("COOLIFY_RESOURCE_UUID")):
        return build(None, env["SOURCE_COMMIT"], "coolify")
    # Dokku sets GIT_REV, but the name is generic, so the source says so.
    if _present(env.get("GIT_REV")):
        return build(None, env["GIT_REV"], "git_rev")
    if root:
        revision = _read_revision(os.path.join(root, "REVISION"))
        if revision is not None:
            return build(None, revision, "revision_file")
    if _present(env.get("ECS_CONTAINER_METADATA_URI_V4")):
        release = ecs(http(env["ECS_CONTAINER_METADATA_URI_V4"]))
        if release:
            return release
    return Release(None, None, "unknown")


def kamal(value: str) -> Release:
    """Kamal sets KAMAL_VERSION in every app container: the git commit by
    default, with "_uncommitted_<random>" added for a dirty working tree, or a
    version you declared. A plain commit is reported as the commit; anything
    else is the version, with the commit it starts with."""
    value = str(value).strip()
    plain_commit = bool(COMMIT_FORMAT.match(value.lower()))
    return build(None if plain_commit else value, value.split("_", 1)[0], "kamal")


def railway(env: Mapping[str, str]) -> Release:
    """Railway sets the commit for deploys from GitHub. Others, such as
    `railway up`, only have a deployment ID, which identifies the release."""
    if _present(env.get("RAILWAY_GIT_COMMIT_SHA")):
        return build(None, env["RAILWAY_GIT_COMMIT_SHA"], "railway")
    return build(env.get("RAILWAY_DEPLOYMENT_ID"), None, "railway")


def fly_tag(image_ref: Optional[str]) -> Optional[str]:
    """Fly.io tags each deploy's image ("registry.fly.io/shop:deployment-01H9…"),
    and the tag identifies the release."""
    name = str(image_ref or "").strip().split("@", 1)[0].split("/")[-1]
    parts = name.split(":", 1)
    tag = parts[1].removeprefix("deployment-") if len(parts) == 2 else ""
    return tag or None


def ecs(body: Optional[str]) -> Optional[Release]:
    """The image tag identifies the release, and is the commit when it looks
    like one. A moving tag such as "latest" can't, so the image digest does."""
    try:
        data = json.loads(body or "")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    name = str(data.get("Image") or "").split("@", 1)[0].split("/")[-1]
    parts = name.split(":", 1)
    tag = parts[1].strip() if len(parts) == 2 else ""
    if COMMIT_FORMAT.match(tag.lower()):
        return build(None, tag, "ecs")
    if tag and tag.lower() not in MOVING_TAGS:
        return build(tag, None, "ecs")
    digest = re.match(r"\Asha256:([0-9a-fA-F]{12})", str(data.get("ImageID") or ""))
    if digest:
        return build(f"sha256:{digest.group(1)}", None, "ecs")
    return None


def fetch_metadata(uri: str) -> Optional[str]:
    """One request at boot to the container's own metadata endpoint, which is
    local, so the timeout is short. None on any failure."""
    try:
        with urllib.request.urlopen(uri, timeout=1) as response:  # noqa: S310 - ECS's own local endpoint
            if 200 <= response.status < 300:
                return response.read().decode("utf-8", "replace")
    except Exception:
        return None
    return None


def build(version, commit, source: str) -> Release:
    """The cloud rejects malformed commits, which would drop every payload, so
    anything that is not a hex SHA is left out."""
    commit = str(commit or "").strip().lower()
    return Release(str(version).strip()[:100] if _present(version) else None,
                   commit if COMMIT_FORMAT.match(commit) else None, source)


def heroku_commit(env: Mapping[str, str]) -> Optional[str]:
    """HEROKU_BUILD_COMMIT (runtime-dyno-build-metadata) replaces the deprecated
    HEROKU_SLUG_COMMIT (runtime-dyno-metadata)."""
    return env.get("HEROKU_BUILD_COMMIT") if _present(env.get("HEROKU_BUILD_COMMIT")) else env.get("HEROKU_SLUG_COMMIT")


def _read_revision(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            return file.read(100)
    except OSError:
        return None


def _present(value) -> bool:
    return bool(str(value or "").strip())
