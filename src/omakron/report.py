"""Validate report-only issue triage output before marking a run successful.

Exit status zero and a non-error CLI result are insufficient on their own.
The supervisor also extracts and validates the report object.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

PRIORITIES = frozenset({"Urgent", "High", "Medium", "Low", "None"})

REQUIRED_FIELDS = (
    "issue_id",
    "link",
    "source_updated_at",
    "current",
    "proposed",
    "reason",
    "next_step",
    "missing_information",
    "possible_duplicate",
)

MIN_REASON_CHARS = 60
MIN_NEXT_STEP_CHARS = 20


def extract_json_object(text: str) -> dict | None:
    """Return the outermost JSON object in ``text``, or ``None``.

    Tolerates a surrounding code fence and stray prose, which the model is told
    not to emit but occasionally does. Anything that is not a JSON object is
    rejected rather than coerced.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def validate_report(obj: object, team_labels: Iterable[str] | None = None) -> list[str]:
    """Return the list of contract violations in ``obj`` (empty means acceptable).

    ``team_labels`` is the label list from the issue snapshot supplied to the
    model. When it is given, every proposed label must come from it; when it is
    ``None`` the label-membership rule is skipped and only the structure is
    checked.
    """
    if not isinstance(obj, dict):
        return ["not a JSON object"]

    problems = [f"missing {key}" for key in REQUIRED_FIELDS if key not in obj]

    for side in ("current", "proposed"):
        value = obj.get(side)
        if not isinstance(value, dict) or "priority" not in value:
            problems.append(f"{side} malformed")
            continue
        labels = value.get("labels")
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            problems.append(f"{side}.labels not a list of strings")
            continue
        if value["priority"] not in PRIORITIES:
            problems.append(f"{side}.priority {value['priority']!r} not in {sorted(PRIORITIES)}")
        if side == "proposed" and team_labels is not None:
            unknown = sorted(set(labels) - set(team_labels))
            if unknown:
                problems.append(f"proposed.labels {unknown} not in team labels")

    if not isinstance(obj.get("reason"), str) or len(obj["reason"]) < MIN_REASON_CHARS:
        problems.append("reason too short to be evidence-based")
    if not isinstance(obj.get("next_step"), str) or len(obj["next_step"]) < MIN_NEXT_STEP_CHARS:
        problems.append("next_step too short to be actionable")
    if not isinstance(obj.get("missing_information"), list):
        problems.append("missing_information not a list")
    dup = obj.get("possible_duplicate")
    if dup is not None and not isinstance(dup, str):
        problems.append("possible_duplicate not string/null")
    return problems
