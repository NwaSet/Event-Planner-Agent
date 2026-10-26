"""Validate schedules and detect exclusive-resource overlaps, without optimization.

Activities contain name, start, end, and optionally resources (a list of exact,
case-sensitive exclusive resource IDs). No resource is inferred from an activity
name; omitted/empty resources do not cause conflicts. Extra JSON fields survive
sorting. Intervals are half-open [start, end): touching boundaries do not overlap.

Datetimes are ISO strings YYYY-MM-DDTHH:MM[:SS[.ffffff]], with an optional Z or
numeric offset +/-HH:MM. A space may replace T. Either every datetime has an
offset, or all are naive local times in the same unspecified timezone. Mixing
these modes is rejected. Aware comparisons and overlap timestamps use UTC.
No geographical timezone or daylight-saving offset is guessed.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import re


_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?"
)


def _datetime(value) -> datetime:
    if not isinstance(value, str) or not _DATETIME.fullmatch(value):
        raise ValueError("Use an ISO datetime with a date and time, optionally including a UTC offset.")
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else parsed
    except (ValueError, OverflowError):
        raise ValueError("Datetime contains an invalid date, time, or UTC offset.") from None


def _inspect(activities: list[dict]) -> tuple[dict, list[tuple]]:
    if not isinstance(activities, list):
        raise ValueError("Activities must be a list of dictionaries.")
    errors, parsed, modes = [], [], set()

    def report(index, field, message):
        errors.append({"index": index, "field": field, "message": message})

    for index, activity in enumerate(activities):
        if not isinstance(activity, dict):
            report(index, "activity", "Activity must be a dictionary.")
            continue
        initial_errors = len(errors)
        try:
            json.dumps(activity, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            report(index, "activity", "Activity fields must be JSON-serializable with finite numbers.")
        if not isinstance(activity.get("name"), str) or not activity["name"].strip():
            report(index, "name", "Activity name must be a non-empty string.")
        times = {}
        for field in ("start", "end"):
            try:
                times[field] = _datetime(activity.get(field))
                modes.add("aware" if times[field].tzinfo is not None else "naive")
            except ValueError as exc:
                report(index, field, str(exc))
        if len(times) == 2:
            start, end = times["start"], times["end"]
            if (start.tzinfo is None) != (end.tzinfo is None):
                report(index, "end", "Start and end must use the same timezone-awareness mode.")
            elif end <= start:
                report(index, "end", "End must be strictly after start; duration must be positive.")
        resources = activity.get("resources", [])
        if (not isinstance(resources, list)
                or any(not isinstance(resource, str) or not resource.strip() for resource in resources)):
            report(index, "resources", "Resources must be a list of non-empty exclusive resource IDs.")
        elif len(set(resources)) != len(resources):
            report(index, "resources", "Resource IDs must not repeat within an activity.")
        if len(errors) == initial_errors:
            parsed.append((index, activity, times["start"], times["end"], set(resources)))
    if len(modes) > 1:
        report(None, "timezone", "A schedule cannot mix timezone-aware and naive datetimes.")
    mode = "mixed" if len(modes) > 1 else next(iter(modes), None)
    return {"valid": not errors, "errors": errors, "activity_count": len(activities), "timezone_mode": mode}, parsed


def validate_schedule(activities: list[dict]) -> dict:
    """Report structural/time errors. Resource overlaps are checked separately."""
    return _inspect(activities)[0]


def _valid_activities(activities: list[dict]) -> list[tuple]:
    result, parsed = _inspect(activities)
    if not result["valid"]:
        raise ValueError("Invalid schedule; use validate_schedule() to inspect the errors.")
    return parsed


def detect_conflicts(activities: list[dict]) -> list[dict]:
    """Return one entry per overlapping pair sharing at least one exclusive resource."""
    parsed = _valid_activities(activities)
    conflicts = []
    for position, (index, activity, start, end, resources) in enumerate(parsed):
        for other_index, other, other_start, other_end, other_resources in parsed[position + 1:]:
            shared = resources & other_resources
            overlap_start, overlap_end = max(start, other_start), min(end, other_end)
            if shared and overlap_start < overlap_end:
                conflicts.append({
                    "activity_indices": [index, other_index], "activity_names": [activity["name"], other["name"]],
                    "resources": sorted(shared), "overlap_start": overlap_start.isoformat(),
                    "overlap_end": overlap_end.isoformat(),
                })
    return conflicts


def sort_activities(activities: list[dict]) -> list[dict]:
    """Return a stable chronological copy, preserving all original activity fields."""
    return [deepcopy(item[1]) for item in sorted(_valid_activities(activities), key=lambda item: item[2])]
