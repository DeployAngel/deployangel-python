"""`deployangel doctor`: what this checkout will report, and what DeployAngel
has seen of the app so far (GET /api/v1/setup), as checks with what to do
about each. It never imports the app, so the schedules the agent reads
(Celery Beat, django-celery-beat, django-crontab, recurring_jobs) are left
to the running agent. Behaves like the Ruby gem's doctor."""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

from deployangel.cli.client import ApiError, NotFound, Unauthorized
from deployangel.config import Configuration
from deployangel.core import release as release_module
from deployangel.metadata import Metadata

TOKEN_HELP = ('Set DEPLOYANGEL_API_TOKEN (a "CLI & coding agents" token from the app\'s Settings) or DEPLOYANGEL_TOKEN '
              "(the app's agent token), then run this again.")
MAX_HOSTS = 5
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Where a Python project declares its dependencies, the locks first, since
# they pin the version.
LOCKS = ("uv.lock", "poetry.lock", "Pipfile.lock")
REQUIREMENTS = ("requirements*.txt", os.path.join("requirements", "*.txt"))
# The schedules the agent reads when the app runs, by the package that keeps
# them; recurring_jobs is the agent's own setting.
SCHEDULERS = (("celery", "Celery Beat's beat_schedule"), ("django-celery-beat", "django-celery-beat's periodic tasks"),
              ("django-crontab", "django-crontab's CRONJOBS"))


@dataclass
class Check:
    # ok, warn, error, or info. Only errors fail the command.
    status: str
    message: str
    fix: Optional[str] = None

    def to_dict(self) -> dict:
        data = {"status": self.status, "message": self.message, "fix": self.fix}
        return {key: value for key, value in data.items() if value is not None}


class Doctor:
    def __init__(self, root: str, client, env: Mapping[str, str]):
        # client is None when no token is set; the local checks still run.
        self.root = root
        self.client = client
        self.env = env
        self.setup: Optional[dict] = None
        self._checks: Optional[list] = None
        self._dependency_files: Optional[list] = None

    @property
    def checks(self) -> list:
        if self._checks is None:
            self.setup, server = self._server_checks()
            checks = [*self._agent_checks(), self._here_check(), self._schedule_check(), *server, self._release_check()]
            self._checks = [check for check in checks if check is not None]
        return self._checks

    @property
    def failed(self) -> bool:
        return any(check.status == "error" for check in self.checks)

    def to_dict(self) -> dict:
        return {"checks": [check.to_dict() for check in self.checks], "setup": self.setup}

    def _agent_checks(self) -> list:
        files = self._dependencies()
        if not files:
            return []
        found = _find(files, "deployangel")
        if found:
            name, version = found
            return [Check("ok", f"Agent: deployangel {version} in {name}" if version else f"Agent: deployangel in {name}")]
        return [Check("error", "The agent isn't in the project's dependencies", self._install_fix(files))]

    def _install_fix(self, files: list) -> str:
        names = [name for name, _ in files]
        if "uv.lock" in names:
            return "Run uv add deployangel."
        if "poetry.lock" in names:
            return "Run poetry add deployangel."
        if "Pipfile" in names or "Pipfile.lock" in names:
            return "Run pipenv install deployangel."
        requirements = [name for name in names if name.endswith(".txt")]
        if requirements:
            name = "requirements.txt" if "requirements.txt" in requirements else requirements[0]
            return f"Add deployangel to {name} and run pip install -r {name}."
        return 'Add "deployangel" to the dependencies in pyproject.toml, then install it.'

    def _schedule_check(self) -> Check:
        files = self._dependencies()
        later = [label for package, label in SCHEDULERS if _find(files, package)] + ["recurring_jobs"]
        listed = later[0] if len(later) == 1 else (" and ".join(later) if len(later) == 2 else ", ".join(later[:-1]) + ", and " + later[-1])
        return Check("info", f"This checkout's scheduled jobs aren't checked here. {listed} {'is' if len(later) == 1 else 'are'} "
                             "read when the app runs.")

    def _server_checks(self):
        """The token's app and what DeployAngel has seen of it."""
        if self.client is None:
            return None, [Check("warn", "No token, so only this checkout was checked", TOKEN_HELP)]
        try:
            setup = self.client.setup()
            if not isinstance(setup, dict):
                raise ApiError("unexpected response")
        except Unauthorized as error:
            return None, [Check("error", f"DeployAngel rejected the token ({error})", TOKEN_HELP)]
        except NotFound:
            return None, [Check("warn", "This DeployAngel server doesn't support doctor yet; only this checkout was checked")]
        except ApiError as error:
            return None, [Check("error", f"Couldn't reach DeployAngel: {error}")]
        checks = [_app_check(setup), _agent_check(setup), _metadata_check(setup), *_gap_checks(setup), _progress_check(setup)]
        return setup, [check for check in checks if check is not None]

    def _here_check(self) -> Optional[Check]:
        """Run where the app runs (`heroku run`, a container shell), the release
        this process would report. On a developer machine it would only be the
        checkout's git HEAD, which says nothing about production, so it's left
        out there. When nothing names the release, the agent falls back to
        the code fingerprint, which it computes from the app's files."""
        try:
            config = Configuration(self.env)
            root = config.root or self.root
            release = release_module.resolve(config, env=self.env, root=root)
            if release.unknown and self._where_the_app_runs():
                release = release_module.code_fingerprint(Metadata(config, root, "production").file_manifest())
            if release.unknown:
                if not self._where_the_app_runs():
                    return None
                return Check("warn", "Here, the agent can't tell which release is running",
                             "Set DEPLOYANGEL_REVISION to the deployed commit, or see https://deployangel.com/docs#containers.")
            if release.source == "code_fingerprint":
                return Check("warn", f"Here, the agent would report release {release.version} (from code_fingerprint), with no commit",
                             "Set DEPLOYANGEL_REVISION to the deployed commit to see each release's commits and pull requests.")
            if release.source != "git_head":
                return Check("ok", f"Here, the agent would report release {release.version or release.commit} (from {release.source})")
        except Exception:
            return None
        return None

    def _where_the_app_runs(self) -> bool:
        return bool(self.env.get("DEPLOYANGEL_TOKEN")) and not self.env.get("DEPLOYANGEL_API_TOKEN")

    def _release_check(self) -> Optional[Check]:
        """How production will know the release, judged from what's reporting
        once anything is, and from this checkout's deploy files before then."""
        if ((self.setup or {}).get("agent") or {}).get("processes"):
            return None
        if self._file(os.path.join("config", "deploy.yml")):
            return Check("ok", "Kamal deploys: the agent reads the release from KAMAL_VERSION")
        if self._file("render.yaml"):
            return Check("ok", "Render deploys: the agent reads the commit from RENDER_GIT_COMMIT")
        if "DEPLOYANGEL_REVISION" in (self._read(os.path.join(".do", "app.yaml")) or ""):
            return Check("ok", "The DigitalOcean app spec passes the commit in DEPLOYANGEL_REVISION")
        dockerfile = self._read("Dockerfile")
        if dockerfile is not None:
            if "DEPLOYANGEL_REVISION" in dockerfile:
                return Check("ok", "The Dockerfile passes the commit in DEPLOYANGEL_REVISION")
            # Platforms can pass it another way, so this is advice until the
            # agent reports: then the release it names settles it.
            return Check("info", "The Dockerfile doesn't set DEPLOYANGEL_REVISION; unless your platform passes the commit, "
                                 "releases won't be named by commit",
                         "Run `deployangel install docker`, then build with --build-arg GIT_SHA=$(git rev-parse HEAD).")
        if self._file("Procfile") or self._file("app.json"):
            return Check("info", "On Heroku, turn on dyno metadata so the agent knows the commit",
                         "heroku labs:enable runtime-dyno-metadata && heroku labs:enable runtime-dyno-build-metadata")
        return None

    def _dependencies(self) -> list:
        """(relative path, contents) of each dependency file in the root, with
        pyproject.toml and Pipfile reduced to their requirements, one a line."""
        if self._dependency_files is None:
            names = list(LOCKS)
            for pattern in REQUIREMENTS:
                names += sorted(os.path.relpath(path, self.root) for path in glob.glob(os.path.join(glob.escape(self.root), pattern)))
            names += ["pyproject.toml", "Pipfile"]
            files = []
            for name in names:
                text = self._read(name)
                if text is not None and name in ("pyproject.toml", "Pipfile"):
                    text = _declared_requirements(text)
                if text is not None:
                    files.append((name.replace(os.sep, "/"), text))
            self._dependency_files = files
        return self._dependency_files

    def _file(self, relative: str) -> bool:
        return os.path.isfile(os.path.join(self.root, relative))

    def _read(self, relative: str) -> Optional[str]:
        path = os.path.join(self.root, relative)
        try:
            with open(path, encoding="utf-8", errors="replace") as file:
                return file.read()
        except OSError:
            return None


def _app_check(setup: dict) -> Check:
    app = setup.get("app") or {}
    return Check("ok", f"App: {app.get('name') or ''} ({app.get('environment') or ''})")


def _agent_check(setup: dict) -> Check:
    agent = setup.get("agent") or {}
    processes = list(agent.get("processes") or [])
    if agent.get("reporting"):
        releases = list(dict.fromkeys(process.get("release") for process in processes))
        release = f", release {releases[0]}" if len(releases) == 1 and releases[0] is not None else ""
        where = f" from {_hosts(processes)}" if processes else ""
        return Check("ok", f"Agent reporting{where}{release}")
    if agent.get("last_received_at"):
        return Check("warn", f"The agent last reported {_time(agent['last_received_at'])}, and nothing since",
                     "Check the app is running, and that DEPLOYANGEL_TOKEN is still set on its processes.")
    return Check("warn", "The agent hasn't reported yet",
                 'Deploy with DEPLOYANGEL_TOKEN set to the app\'s agent token (Settings, Tokens: "Deployed application").')


def _metadata_check(setup: dict) -> Optional[Check]:
    metadata = setup.get("metadata")
    if not metadata:
        return None
    jobs = list(setup.get("scheduled_jobs") or [])
    problems = [job for job in jobs if job.get("status") in ("overdue", "failed")]
    problem_text = (f", {len(problems)} needing attention ("
                    + ", ".join(f"{job.get('label')} {job.get('status')}" for job in problems[:3]) + ")") if problems else ""
    return Check("warn" if problems else "ok",
                 f"Reported: {_count(metadata.get('routes'), 'route')}, {_count(metadata.get('job_classes'), 'job class', 'job classes')}, "
                 f"{_count(metadata.get('schedules'), 'scheduled job')}{problem_text}",
                 "See them on the app page, under Scheduled jobs." if problems else None)


def _gap_checks(setup: dict) -> list:
    return [Check("warn", gap.get("title"), gap.get("fix")) for gap in setup.get("gaps") or []]


def _progress_check(setup: dict) -> Optional[Check]:
    deployments = setup.get("deployments") or {}
    latest = deployments.get("latest")
    if setup.get("warm_up_ends_at"):
        message = (f"Learning what's normal for this app until {_time(setup['warm_up_ends_at'])}; "
                   "deploys before then are checked, not cleared")
    elif latest:
        message = (f"{_count(deployments.get('count'), 'deploy')} so far; the latest, {latest.get('label')}, "
                   f"is {latest.get('verdict') or latest.get('state')}")
    elif (setup.get("agent") or {}).get("last_received_at"):
        message = "No deploys yet: your next deploy gets the first verdict"
    else:
        return None
    return Check("info", message)


def _declared_requirements(toml: str) -> Optional[str]:
    """The requirements a pyproject.toml or Pipfile declares, one a line: the
    project's dependencies and extras, dependency groups, and Poetry's and
    Pipenv's package tables. None when it declares none, as a pyproject.toml
    that holds only tool settings doesn't."""
    sections = re.split(r"^\[([^\]\n]+)\][ \t]*(?:#.*)?$", toml, flags=re.MULTILINE)
    requirements, declares = [], False
    for header, body in zip(sections[1::2], sections[2::2]):
        header = header.strip()
        if header == "project":
            array = re.search(r"^\s*dependencies\s*=\s*\[((?:\"[^\"]*\"|'[^']*'|[^\]\"'])*)\]", body, re.MULTILINE)
            if not array:
                continue
            body = array.group(1)
        elif re.fullmatch(r"tool\.poetry\.(?:group\.[^.]+\.)?(?:dev-)?dependencies|packages|dev-packages", header):
            requirements += re.findall(r"^\s*[\"']?([A-Za-z0-9][\w.-]*)[\"']?\s*=", body, re.MULTILINE)
            declares = True
            continue
        elif header not in ("project.optional-dependencies", "dependency-groups"):
            continue
        declares = True
        requirements += [double or single for double, single in re.findall(r"\"([^\"\n]*)\"|'([^'\n]*)'", body)]
    return "\n".join(requirements) if declares else None


def _find(files: list, package: str):
    """(file, pinned version or None) for the first file that names the
    package, preferring one that pins it; None when none does. Names compare
    as PyPI does: case-insensitive, with -, _, and . alike."""
    name = r"[-_.]+".join(re.escape(part) for part in re.split(r"[-_.]+", package))
    requirement = re.compile(rf"^\s*{name}(?:\[[^\]]*\])?\s*(?:===?\s*([\w.+!-]+))?\s*(?=[<>=!~;@#,\s]|$)",
                             re.IGNORECASE | re.MULTILINE)
    first = None
    for path, text in files:
        found, version = False, None
        base = os.path.basename(path)
        if base in ("uv.lock", "poetry.lock"):
            match = re.search(rf'^name = "{name}"\s*\nversion = "([^"]+)"', text, re.IGNORECASE | re.MULTILINE)
            found, version = bool(match), match and match.group(1)
        elif base == "Pipfile.lock":
            try:
                packages = json.loads(text).get("default") or {}
            except (ValueError, AttributeError):
                packages = {}
            entry = next((value for key, value in packages.items() if re.fullmatch(name, key, re.IGNORECASE)), None)
            found = entry is not None
            if isinstance(entry, dict):
                version = str(entry.get("version") or "").lstrip("=") or None
        else:
            match = requirement.search(text)
            found, version = bool(match), match and match.group(1)
        if found and version:
            return path, version
        if found and first is None:
            first = (path, None)
    return first


def _hosts(processes: list) -> str:
    names = [f"{process.get('host') or ''} ×{process.get('processes')}" if _int(process.get("processes")) > 1
             else str(process.get("host") or "") for process in processes]
    total = sum(_int(process.get("processes")) for process in processes)
    listed = ", ".join(names[:MAX_HOSTS]) + (", and others" if len(names) > MAX_HOSTS else "")
    return f"{_count(total, 'process', 'processes')} ({listed})"


def _time(iso) -> str:
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    moment = (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    return f"{MONTHS[moment.month - 1]} {moment.day}, {moment:%H:%M} UTC"


def _count(number, singular: str, plural: Optional[str] = None) -> str:
    number = _int(number)
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
