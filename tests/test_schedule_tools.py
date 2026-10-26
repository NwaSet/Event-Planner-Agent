"""Datetime, resource-overlap, and stable sorting contracts."""

from copy import deepcopy
import json

import pytest

from tools.schedule_tools import validate_schedule, detect_conflicts, sort_activities


def activity(name="Setup", start="2026-10-26T10:00", end="2026-10-26T11:00", resources=None, **extra):
    record = {"name": name, "start": start, "end": end, **extra}
    if resources is not None:
        record["resources"] = resources
    return record


def test_valid_naive_and_aware_schedules():
    assert validate_schedule([activity()]) == {
        "valid": True, "errors": [], "activity_count": 1, "timezone_mode": "naive",
    }
    assert validate_schedule([activity(start="2026-10-26T10:00+01:00", end="2026-10-26T11:00+01:00")])["timezone_mode"] == "aware"
    assert validate_schedule([activity(start="2026-10-26T10:00:00.123456Z", end="2026-10-26T10:00:00.123457Z")])["valid"]


def test_empty_schedule():
    assert validate_schedule([]) == {"valid": True, "errors": [], "activity_count": 0, "timezone_mode": None}
    assert detect_conflicts([]) == sort_activities([]) == []


@pytest.mark.parametrize("record", [
    None, {}, {"name": "A"}, activity(name=""), activity(name="  "), activity(name=123),
    activity(start="2026-10-26"), activity(start="10:00"), activity(start="2026-02-30T10:00"),
    activity(end="2026-10-26T10:00"), activity(end="2026-10-26T09:00"), activity(start="2026-10-26T24:00"),
    activity(start="2026-10-26T10:00+01:60"), activity(start="2026-10-26T10:00+24:00"),
    activity(start=None), activity(resources="room"), activity(resources=[None]),
    activity(resources=[""]), activity(resources=["room", "room"]), activity(extra=object()),
    activity(extra=float("nan")),
])
def test_invalid_activities_report_errors_and_cannot_be_sorted_or_compared(record):
    result = validate_schedule([record])
    assert not result["valid"] and result["errors"]
    assert all(error["index"] in {0, None} for error in result["errors"])
    with pytest.raises(ValueError, match="Invalid schedule"):
        sort_activities([record])
    with pytest.raises(ValueError, match="Invalid schedule"):
        detect_conflicts([record])


@pytest.mark.parametrize("value", [None, {}, "schedule", 1])
def test_invalid_container(value):
    for function in [validate_schedule, detect_conflicts, sort_activities]:
        with pytest.raises(ValueError):
            function(value)


def test_mixed_timezone_modes_rejected_across_activities_and_within_one():
    aware = activity(start="2026-10-26T10:00Z", end="2026-10-26T11:00Z")
    for records in [[activity(), aware], [activity(start="2026-10-26T10:00Z")]]:
        result = validate_schedule(records)
        assert not result["valid"] and result["timezone_mode"] == "mixed"
        assert any(error["field"] == "timezone" for error in result["errors"])


def test_simultaneous_activities_need_shared_exclusive_resources_to_conflict():
    records = [activity("A", resources=["room-a"]), activity("B", resources=["room-b"]),
               activity("No explicit resource"), activity("No resource list", resources=[])]
    assert detect_conflicts(records) == []
    records.append(activity("Case-sensitive resource", resources=["ROOM-A"]))
    assert detect_conflicts(records) == []


def test_resource_conflict_pair_fields_and_non_mutation():
    records = [activity("Setup", resources=["room", "projector"]),
               activity("Talk", "2026-10-26T10:30", "2026-10-26T12:00", ["projector", "room"])]
    original = deepcopy(records)
    assert detect_conflicts(records) == [{
        "activity_indices": [0, 1], "activity_names": ["Setup", "Talk"],
        "resources": ["projector", "room"], "overlap_start": "2026-10-26T10:30:00", "overlap_end": "2026-10-26T11:00:00",
    }]
    assert records == original
    assert validate_schedule(records)["valid"]  # Structural validation is distinct from conflicts.


def test_adjacent_intervals_are_allowed_and_nested_intervals_conflict():
    records = [activity(resources=["room"]), activity("Next", "2026-10-26T11:00", "2026-10-26T12:00", ["room"])]
    assert detect_conflicts(records) == []
    records.append(activity("Inside", "2026-10-26T10:15", "2026-10-26T10:45", ["room"]))
    conflicts = detect_conflicts(records)
    assert len(conflicts) == 1
    assert conflicts[0]["activity_indices"] == [0, 2]


def test_each_pair_reported_once_and_duplicate_names_remain_identifiable():
    records = [activity("Same name", resources=["room"]) for _ in range(3)]
    assert [conflict["activity_indices"] for conflict in detect_conflicts(records)] == [[0, 1], [0, 2], [1, 2]]


def test_offsets_compared_as_instants_and_overlaps_reported_in_utc():
    records = [activity("A", "2026-10-26T11:00+02:00", "2026-10-26T12:00+02:00", ["room"]),
               activity("B", "2026-10-26T09:30Z", "2026-10-26T10:30Z", ["room"])]
    result = detect_conflicts(records)
    assert result[0]["overlap_start"] == "2026-10-26T09:30:00+00:00"
    assert result[0]["overlap_end"] == "2026-10-26T10:00:00+00:00"


def test_overnight_and_explicit_daylight_saving_offsets():
    assert validate_schedule([activity(start="2026-10-26T23:00+01:00", end="2026-10-27T01:00+01:00")])["valid"]
    assert validate_schedule([activity(start="2026-10-25T02:30+02:00", end="2026-10-25T02:15+01:00")])["valid"]
    assert not validate_schedule([activity(start="2026-10-26T23:00", end="2026-10-26T01:00")])["valid"]


def test_sort_is_stable_chronological_and_returns_deep_copies():
    records = [activity("Later", "2026-10-26T12:00Z", "2026-10-26T13:00Z"),
               activity("Earlier", "2026-10-26T12:00+02:00", "2026-10-26T13:00+02:00", notes={"tags": ["original"]}),
               activity("Tie", "2026-10-26T10:00Z", "2026-10-26T11:00Z")]
    before = deepcopy(records)
    sorted_records = sort_activities(records)
    assert [record["name"] for record in sorted_records] == ["Earlier", "Tie", "Later"]
    assert sorted_records[0]["start"] == "2026-10-26T12:00+02:00"
    sorted_records[0]["notes"]["tags"].append("changed")
    assert records == before
    json.dumps(sorted_records, allow_nan=False)
