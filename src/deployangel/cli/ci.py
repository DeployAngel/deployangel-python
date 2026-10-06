"""Recognizes common CI systems from their environment variables, so
`deployangel release` needs no arguments there: the commit, a build label,
the provider, and a link back to the run. Inside a Kamal hook, the release
is Kamal's, linked to the CI run when Kamal runs in CI."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping, Optional

from deployangel.core import release as release_module


@dataclass
class CiEnvironment:
    provider: str
    commit: Optional[str]
    version: Optional[str]
    raw_source_url: Optional[str] = None

    @property
    def source_url(self) -> Optional[str]:
        """Only https links are kept; the cloud rejects anything else."""
        url = self.raw_source_url or ""
        return url if url.startswith("https://") else None

    def to_dict(self) -> dict:
        values = asdict(self)
        values["source_url"] = self.source_url
        del values["raw_source_url"]
        return values


def detect(env: Mapping[str, str]) -> Optional[CiEnvironment]:
    ci = _detect_ci(env)
    if not _present(env.get("KAMAL_VERSION")):
        return ci
    kamal = release_module.kamal(env["KAMAL_VERSION"])
    return CiEnvironment("kamal", kamal.commit, kamal.version, ci.source_url if ci else None)


def _detect_ci(env: Mapping[str, str]) -> Optional[CiEnvironment]:
    if env.get("GITHUB_ACTIONS") == "true":
        run_url = None
        if env.get("GITHUB_RUN_ID"):
            run_url = f"{env.get('GITHUB_SERVER_URL')}/{env.get('GITHUB_REPOSITORY')}/actions/runs/{env['GITHUB_RUN_ID']}"
        return CiEnvironment("github_actions", env.get("GITHUB_SHA"), _label("run", env.get("GITHUB_RUN_NUMBER")), run_url)
    if env.get("GITLAB_CI") == "true":
        return CiEnvironment("gitlab_ci", env.get("CI_COMMIT_SHA"), _label("pipeline", env.get("CI_PIPELINE_IID")),
                             env.get("CI_PIPELINE_URL"))
    if env.get("CIRCLECI") == "true":
        return CiEnvironment("circleci", env.get("CIRCLE_SHA1"), _label("build", env.get("CIRCLE_BUILD_NUM")),
                             env.get("CIRCLE_BUILD_URL"))
    if env.get("BUILDKITE") == "true":
        return CiEnvironment("buildkite", env.get("BUILDKITE_COMMIT"), _label("build", env.get("BUILDKITE_BUILD_NUMBER")),
                             env.get("BUILDKITE_BUILD_URL"))
    return None


def _label(prefix: str, number: Optional[str]) -> Optional[str]:
    return f"{prefix}-{number}" if _present(number) else None


def _present(value) -> bool:
    return bool(str(value or "").strip())
