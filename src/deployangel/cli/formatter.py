"""Plain-text and Markdown rendering of the verdict document for people.
Everything shown comes from the document; nothing is inferred here. Mirrors
the Ruby gem's formatter, so both commands read the same."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

LABELS = {
    "http_5xx_rate": "HTTP 5xx rate", "route_5xx_rate": "Route 5xx rate", "p95_latency": "p95 latency",
    "job_failure_rate": "Job failure rate", "job_duration": "Job duration p95", "queue_latency": "Queue latency p95",
    "new_fingerprint": "New exception", "fingerprint_amplification": "Exception amplification",
    "missing_recurring_job": "Recurring job", "first_use_failure": "First-use failure", "external_check": "External check",
    "checkpoint_rate": "Checkpoint",
}
DURATIONS = ("p95_latency", "job_duration", "queue_latency")
COUNTS = ("new_fingerprint", "checkpoint_rate")
# Exercise plan statuses with something worth running now.
EXERCISABLE = ("exercisable", "waiting_for_activity")
REASONS = {"normally_active": "normally active", "changed_in_release": "changed in this release",
           "rarely_used": "rarely used", "critical_flow": "critical flow"}
VERDICT_WORDS = {"verified": "cleared", "failed": "failed", "inconclusive": "not cleared"}


def verification(document: dict) -> str:
    lines = [statement(document), status_line(document), *notes(document)]
    found = problems(document)
    if found:
        lines.append("Findings:")
        lines.extend(f"  {f['status'].ljust(8)} {finding_line(f)}" for f in found)
    gathering = gathering_line(document)
    if gathering:
        lines.append(f"{gathering} (--format=json for details)")
    for title, items in sections(document):
        lines.append(f"{title}:")
        lines.extend(f"  {item}" for item in items)
    if document.get("dashboard_url"):
        lines.append(document["dashboard_url"])
    return "\n".join(line for line in lines if line is not None)


def markdown(document: dict) -> str:
    """The same document as GitHub-flavored Markdown, for a CI job's summary."""
    lines = [f"### DeployAngel: {release_name(document)} {outcome(document)}", "", str(statement(document) or ""),
             "", status_line(document), *[f"\n{note}" for note in notes(document)]]
    found = problems(document)
    if found:
        lines += ["", "| Status | Finding |", "| --- | --- |"]
        lines.extend(f"| {f['status']} | {_cell(finding_line(f))} |" for f in found)
    gathering = gathering_line(document)
    if gathering:
        lines += ["", gathering]
    for title, items in sections(document):
        lines += ["", f"**{title}**", ""]
        lines.extend(f"- {item}" for item in items)
    if document.get("dashboard_url"):
        lines += ["", f"[Open in DeployAngel]({document['dashboard_url']})"]
    return "\n".join(line for line in lines if line is not None) + "\n"


def statement(document: dict) -> Optional[str]:
    return (document.get("clearance") or {}).get("statement") or document.get("summary")


def status_line(document: dict) -> str:
    v = document.get("verification") or {}
    parts = [f"State: {v.get('state')}"]
    if v.get("verdict"):
        parts.append(f"Verdict: {v['verdict']}")
    if v.get("confidence"):
        parts.append(f"Confidence: {v['confidence']}")
    if v.get("coverage") is not None:
        parts.append(f"Coverage: {_round(v['coverage'] * 100)}%")
    return " · ".join(parts)


def notes(document: dict) -> list:
    v = document.get("verification") or {}
    found = []
    check = v.get("initial_check")
    if check and v.get("verdict") is None:
        found.append(f"Initial check: {check['result'].replace('_', ' ')}. Not cleared yet.")
    if v.get("expected_clearance_at") and v.get("verdict") is None:
        found.append(f"Expected clearance: {v['expected_clearance_at']}")
    source = (document.get("deployment") or {}).get("promoted_from")
    if source:
        result = VERDICT_WORDS.get(source.get("verdict"), "still verifying")
        found.append(f"Promoted from {source.get('environment')} {source.get('version') or str(source.get('commit') or '')[:7]} ({result})")
    return found


def problems(document: dict) -> list:
    return [f for f in document.get("findings") or [] if f.get("status") in ("failing", "warning")]


def gathering_line(document: dict) -> Optional[str]:
    gathering = sum(1 for f in document.get("findings") or [] if f.get("status") == "insufficient_data")
    if not gathering:
        return None
    return f"{gathering} signal{'' if gathering == 1 else 's'} still gathering data"


def sections(document: dict) -> list:
    """Titled lists that follow the findings, each item already a line."""
    v = document.get("verification") or {}
    clearance = document.get("clearance") or {}
    changed = [item for item in document.get("rare_items_pending") or [] if item.get("changed_in_release") == "changed"]
    exceptions = []
    for e in document.get("exceptions") or []:
        sources = e.get("sources")
        suffix = f" {', '.join(sources.keys())}" if isinstance(sources, dict) and sources else ""
        exceptions.append(f"{e.get('exception_class')} in {e.get('top_frame')} ({e.get('count')}x){suffix}")
    flows = []
    for flow in document.get("critical_flows") or []:
        note = " (changed in this release)" if flow.get("changed_in_release") else ""
        flows.append(f"{flow.get('name')}: {str(flow.get('status')).replace('_', ' ')}{note}")
    found = [
        ("New exceptions", exceptions),
        ("Missing evidence", [] if v.get("verdict") == "failed" else list(clearance.get("missing_evidence") or [])),
        ("Not observable", [f"{item.get('label')} ({item.get('reason')})" for item in clearance.get("not_observable") or []]),
        ("Still watching", [f"{e.get('job_class')} (expected by {e.get('expected_by')})" for e in clearance.get("still_watching") or []]),
        ("Critical flows", flows),
        ("Changed but not yet exercised", [item.get("key") for item in changed]),
        ("To clear sooner, exercise (deployangel plan for details)", exercisable_items(document)[:5]),
        ("Late regressions", [late.get("summary") for late in document.get("late_regressions") or []]),
    ]
    return [(title, items) for title, items in found if items]


def exercise_plan(document: dict) -> str:
    """The release's exercise plan, for `deployangel plan`."""
    plan = document.get("exercise_plan") or {}
    lines = [f"{release_name(document)}: {plan.get('summary') or 'No exercise plan in this response; update the server.'}"]
    shortfall = shortfall_lines(plan.get("shortfall") or {})
    if shortfall:
        lines.append("Short of:")
        lines.extend(f"  {line}" for line in shortfall)
    items = plan.get("items") or []
    if plan.get("status") in EXERCISABLE:
        _item_group(lines, "Needed to clear, exercise against production:", [item for item in items if needed(item)])
        _item_group(lines, "Also worth running, not needed to clear (changed or rarely used, watched on first use):",
                    [item for item in items if not needed(item)])
    else:
        _item_group(lines, "Optional:", items)
    if any(item.get("mutating") for item in items):
        lines.append("Use a test account, or ask first, for routes marked [changes data].")
    if plan.get("report_with"):
        lines.append(f"Then report it: {plan['report_with']}")
    return "\n".join(lines)


def exercisable_items(document: dict) -> list:
    """Only what clearance waits on: the rest is in `deployangel plan`."""
    plan = document.get("exercise_plan") or {}
    if plan.get("status") not in EXERCISABLE:
        return []
    return [item_line(item) for item in plan.get("items") or [] if needed(item)]


def needed(item: dict) -> bool:
    """Whether clearance waits on the item. Servers older than the needed
    flag listed normally active items first, so count those."""
    return bool(item["needed"]) if "needed" in item else item.get("reason") == "normally_active"


def _item_group(lines: list, heading: str, items: list) -> None:
    if items:
        lines.append(heading)
        lines.extend(f"  {item_line(item)}" for item in items)


def shortfall_lines(shortfall: dict) -> list:
    lines = []
    requests = shortfall.get("requests")
    if requests:
        rule = " (low-traffic rule)" if shortfall.get("rule") == "low_volume" else ""
        lines.append(f"requests: {requests.get('have')} of {requests.get('need')}{rule}")
    for key, value in shortfall.items():
        if key.startswith("routes_run_"):
            times = "".join(ch for ch in key if ch.isdigit())
            lines.append(f"routes run {times}+ times: {value.get('have')} of the {value.get('need')} needed "
                         f"({value.get('of')} normally active)")
    coverage = shortfall.get("coverage")
    if coverage:
        lines.append(f"coverage: {_round(float(coverage.get('have') or 0) * 100)}% of {_round(float(coverage.get('need') or 0) * 100)}%")
    jobs = shortfall.get("jobs")
    if jobs:
        if jobs.get("classes_not_run") is not None:
            lines.append(f"jobs not run yet: {', '.join(jobs['classes_not_run'])}")
        else:
            attempts = jobs.get("attempts") or {}
            lines.append(f"job attempts: {attempts.get('have')} of {attempts.get('need')}")
    if shortfall.get("elevated"):
        lines.append(f"elevated, review before exercising more: {', '.join(shortfall['elevated'])}")
    if shortfall.get("critical_flows"):
        lines.append(f"critical flows not run: {', '.join(shortfall['critical_flows'])}")
    return lines


def item_line(item: dict) -> str:
    runs = f"run {item.get('runs')} of {item.get('runs_needed')}" if int(item.get("runs_needed") or 0) > 1 else "not run yet"
    if item.get("triggered_by") == "app_behavior":
        runs = "runs when the app starts it"
    parts = [str(item.get("key")), f"({REASONS.get(item.get('reason'), item.get('reason'))}, {runs})"]
    if item.get("mutating"):
        parts.append("[changes data]")
    if item.get("checked_by"):
        parts.append(f"checked by {', '.join(item['checked_by'])}")
    return " ".join(parts)


def release_name(document: dict) -> str:
    deployment = document.get("deployment") or {}
    return deployment.get("version") or str(deployment.get("commit") or "")[:7]


def outcome(document: dict) -> str:
    v = document.get("verification") or {}
    verdict = v.get("verdict")
    if verdict in VERDICT_WORDS:
        return VERDICT_WORDS[verdict]
    result = (v.get("initial_check") or {}).get("result")
    if result == "warnings":
        return "has warnings, not cleared yet"
    if result is None:
        return "is still being verified"
    return "has no problems so far, not cleared yet"


def exception(details: dict) -> str:
    lines = [f"{details.get('exception_class')}: {details.get('message')}",
             f"Fingerprint {details.get('fingerprint')} · first seen {details.get('first_seen_at')} ({details.get('first_seen_release')})",
             f"{details.get('occurrences_last_24h')} occurrences in the last 24 hours"]
    lines.extend(f"  {frame}" for frame in details.get("backtrace") or [])
    return "\n".join(lines)


def finding_line(finding: dict) -> str:
    signal = finding.get("signal")
    label = f"{LABELS.get(signal, signal)} on {finding.get('scope')}"
    if signal in COUNTS:
        return f"{label}: {int(finding.get('observed_value') or 0)} occurrences"
    if signal == "missing_recurring_job":
        # Its value is the job's interval in seconds, not a rate.
        ran = "ran" if finding.get("status") == "pass" else "didn't run"
        return f"{label}: {ran}, {finding.get('threshold')}"
    if finding.get("baseline_value") is None and finding.get("observed_value") is None:
        return f"{label}: {finding.get('threshold')}"
    samples = f" ({finding['observed_n']} samples)" if finding.get("observed_n") is not None else ""
    return f"{label}: {_value(finding, finding.get('baseline_value'))} -> {_value(finding, finding.get('observed_value'))}{samples}"


def _value(finding: dict, raw) -> str:
    if raw is None:
        return "n/a"
    if finding.get("signal") in COUNTS:
        return str(int(raw))
    if finding.get("signal") in DURATIONS:
        return f"{_round(float(raw))} ms"
    return f"{_round(float(raw) * 100, 2)}%"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _round(value: float, digits: int = 0):
    """Rounds halves away from zero, as Ruby does, so both commands print the
    same numbers. Whole numbers print without a decimal point."""
    rounded = Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return int(rounded) if digits == 0 else float(rounded)
