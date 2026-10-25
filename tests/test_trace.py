"""Offline contract and security tests for the tracing subsystem."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from io import StringIO
import json
from pathlib import Path
from uuid import UUID

import pytest
from rich.console import Console
from rich.style import Style

from model import trace as tracing


@pytest.fixture(autouse=True)
def isolated_tracing(monkeypatch):
    output, errors = StringIO(), StringIO()
    monkeypatch.setattr(tracing, "console", Console(file=output, width=1200, color_system=None))
    monkeypatch.setattr(tracing, "_error_console", Console(file=errors, width=1200))
    monkeypatch.setattr(tracing, "_colors_agents", {})
    monkeypatch.setattr(tracing, "_listeners", [])
    monkeypatch.setattr(tracing, "_agent_width", 12)
    monkeypatch.setattr(tracing, "_console_enabled", True)
    return output, errors


def test_event_creation_and_utc_timestamp():
    before = datetime.now(timezone.utc)
    event = tracing.trace("BudgetAgent", "ACTION", "Évaluer le budget", "conv_001", {"budget": 5000})
    after = datetime.now(timezone.utc)
    assert isinstance(event, tracing.TraceEvent)
    assert event.agent == "BudgetAgent"
    assert event.action == "ACTION"
    assert event.summary == "Évaluer le budget"
    assert event.conversation_id == "conv_001"
    assert event.metadata == {"budget": 5000}
    stamp = datetime.fromisoformat(event.timestamp)
    assert stamp.utcoffset() == timedelta(0)
    assert before <= stamp <= after
    assert set(event.to_dict()) == {
        "timestamp", "conversation_id", "agent", "action", "summary", "metadata",
    }


@pytest.mark.parametrize("kind", tracing.COLORS_TYPES)
def test_all_event_types(kind):
    assert tracing.trace("Agent", kind, "Activity").action == kind


@pytest.mark.parametrize("agent,kind", [("", "INFO"), ("  ", "INFO"), (None, "INFO"),
                                          ("Agent", "UNKNOWN"), ("Agent", []), ("Agent", "info")])
def test_invalid_identity_or_type(agent, kind):
    with pytest.raises(ValueError):
        tracing.trace(agent, kind, "Activity")


@pytest.mark.parametrize("kwargs", [{"summary": 5}, {"metadata": []}, {"conversation_id": 5}])
def test_invalid_payload(kwargs):
    with pytest.raises(TypeError):
        tracing.trace("Agent", "INFO", **({"summary": "Activity"} | kwargs))


def test_json_normalization_and_independent_snapshot():
    class Status(Enum):
        READY = "ready"

    @dataclass
    class Result:
        city: str
        api_key: str

    metadata = {
        "date": date(2026, 10, 25), "price": Decimal("0.1234567890123456789"),
        "path": Path("venues.json"), "status": Status.READY, "items": (1, 2),
        "unique": {3, 4}, "bytes": b"abc", "infinite": float("inf"),
        "id": UUID(int=0), "result": Result("Namur", "hidden-value"),
        "nested": [{"capacity": 150}],
    }
    event = tracing.trace("Agent", "INFO", "Ready", metadata=metadata)
    payload = json.loads(json.dumps(event.to_dict(), ensure_ascii=False, allow_nan=False))
    assert payload["metadata"]["price"] == "0.1234567890123456789"
    assert payload["metadata"]["date"] == "2026-10-25"
    assert payload["metadata"]["status"] == "ready"
    assert payload["metadata"]["items"] == [1, 2]
    assert sorted(payload["metadata"]["unique"]) == [3, 4]
    assert payload["metadata"]["bytes"] == {"encoding": "hex", "data": "616263"}
    assert payload["metadata"]["result"] == {"city": "Namur", "api_key": "[REDACTED]"}
    payload["metadata"]["nested"][0]["capacity"] = 10
    metadata["nested"][0]["capacity"] = 20
    assert event.metadata["nested"][0]["capacity"] == 150


def test_cycles_and_unsupported_objects():
    class Unsafe:
        def __repr__(self):
            raise AssertionError("Do not inspect arbitrary object representations.")

    cyclic = {}
    cyclic["self"] = cyclic
    event = tracing.observation("Agent", [cyclic, Unsafe()])
    assert event.metadata["result"] == [{"self": "[CIRCULAR]"}, "[UNSUPPORTED OBJECT]"]
    json.dumps(event.to_dict(), allow_nan=False)


def test_direct_dataclass_serialization():
    event = tracing.TraceEvent("2026-10-25T00:00:00+00:00", None, "A", "INFO", "Ready",
                               {"password": "hidden-value", "date": date(2026, 10, 25)})
    payload = event.to_dict()
    assert payload["metadata"]["password"] == "[REDACTED]"
    json.dumps(payload, allow_nan=False)


def test_agent_color_order_consistency_and_wrap():
    for index in range(len(tracing.AGENTS_PALETTE) * 2):
        assert tracing.agent_color(f"Agent{index}") == tracing.AGENTS_PALETTE[index % 8]
        assert tracing.agent_color("Agent0") == tracing.AGENTS_PALETTE[0]


@pytest.mark.parametrize("limit,expected", [(0, ""), (1, "."), (2, ".."), (3, "..."),
                                             (4, "a..."), (6, "abcdef"), (7, "abcdef")])
def test_text_truncation(limit, expected):
    assert tracing.reduce_text("abcdef", limit) == expected
    assert len(expected) <= limit
    assert tracing.reduce_text("", limit) == ""


def test_default_and_negative_truncation():
    assert tracing.reduce_text("x" * 401) == "x" * 397 + "..."
    with pytest.raises(ValueError):
        tracing.reduce_text("", -1)


def test_multiple_listeners_duplicates_removal_and_clear():
    first, second = [], []
    tracing.define_listener(first.append)
    tracing.define_listener(first.append)
    tracing.define_listener(second.append)
    event = tracing.info("Agent", "Ready")
    assert first == second == [event]
    tracing.remove_listener(first.append)
    tracing.remove_listener(first.append)
    next_event = tracing.info("Agent", "Next")
    assert first == [event]
    assert second == [event, next_event]
    tracing.clear_listeners()
    tracing.info("Agent", "Last")
    assert len(second) == 2
    with pytest.raises(TypeError):
        tracing.define_listener(None)


def test_listener_failure_isolated_and_safe(isolated_tracing):
    output, errors = isolated_tracing
    events = []

    def failing(event):
        raise RuntimeError("Unlabeled sensitive-value must not be printed")

    tracing.disable_console()
    tracing.define_listener(failing)
    tracing.define_listener(events.append)
    event = tracing.info("Agent", "Ready")
    assert events == [event]
    assert errors.getvalue().count("Trace listener failed") == 1
    assert "sensitive-value" not in errors.getvalue()
    assert "Traceback" not in errors.getvalue()
    assert output.getvalue() == ""


def test_subscription_changes_apply_to_next_event():
    events = []

    def unsubscribe(event):
        tracing.remove_listener(events.append)

    tracing.define_listener(unsubscribe)
    tracing.define_listener(events.append)
    first = tracing.info("Agent", "First")
    tracing.info("Agent", "Second")
    assert events == [first]


def test_rich_literal_output_and_alignment(isolated_tracing):
    output, _ = isolated_tracing
    tracing.info("[red]A[/red]", "[bold]Bonjour[/bold]")
    tracing.info("VenueAgent", "Ready")
    lines = output.getvalue().splitlines()
    assert "[red]A[/red]" in lines[0]
    assert "[bold]Bonjour[/bold]" in lines[0]
    assert lines[0].index("[INFO]") == lines[1].index("[INFO]")


def test_rich_styles(monkeypatch):
    rendered = []
    monkeypatch.setattr(tracing.console, "print", lambda text, **kwargs: rendered.append(text))
    tracing.trace("VenueAgent", "TOOL", "Searching")
    line = rendered[0]
    assert Style.parse(line.style) == Style.parse(tracing.agent_color("VenueAgent"))
    assert any(span.style == tracing.COLORS_TYPES["TOOL"] for span in line.spans)


def test_disabled_presentation_still_delivers_events(isolated_tracing):
    output, _ = isolated_tracing
    events = []
    tracing.define_listener(events.append)
    tracing.disable_console()
    first = tracing.info("Agent", "Hidden")
    tracing.title("Hidden title")
    tracing.panel("Hidden panel")
    assert output.getvalue() == ""
    assert events == [first]
    tracing.enable_console()
    tracing.info("Agent", "Visible")
    assert "Visible" in output.getvalue()


@pytest.mark.parametrize("helper,kind", [(tracing.think, "THINK"), (tracing.response, "RESPONSE"),
                                         (tracing.human, "HUMAN"), (tracing.error, "ERROR"),
                                         (tracing.info, "INFO")])
def test_simple_helpers(helper, kind):
    event = helper("Agent", "Préparer la réunion", "conv_001")
    assert (event.action, event.summary, event.conversation_id) == (
        kind, "Préparer la réunion", "conv_001",
    )


@pytest.mark.parametrize("key", ["api_key", "Authorization", "password", "secret", "access_token",
                                  "refresh_token", "openrouter_api_key", "apiKey", "clientSecret",
                                  "X-API-Key", "headers", "request_headers", "Cookie"])
def test_recursive_metadata_sanitization(key):
    source = {"nested": [{key: "hidden-value"}], "prompt_tokens": 1200,
              "completion_tokens": 350, "total_tokens": 1550}
    event = tracing.trace("Agent", "TOKEN", "Usage", metadata=source)
    assert event.metadata["nested"][0][key] == "[REDACTED]"
    assert source["nested"][0][key] == "hidden-value"
    assert event.metadata["prompt_tokens"] == 1200
    assert event.metadata["completion_tokens"] == 350
    assert event.metadata["total_tokens"] == 1550
    assert "hidden-value" not in json.dumps(event.to_dict())


@pytest.mark.parametrize("text", ["api_key=hidden-value", "Authorization: Bearer hidden-value",
                                   "password='hidden-value'", 'secret="hidden-value"',
                                   "Failed with Bearer hidden-value", "sk-hidden-value",
                                   'Failed with {"password": "hidden-value"}'])
def test_free_form_redaction(text, isolated_tracing):
    event = tracing.error("Agent", text)
    assert "hidden-value" not in event.summary
    assert "hidden-value" not in isolated_tracing[0].getvalue()
    assert "[REDACTED]" in event.summary
    assert tracing._sanitize_text(event.summary) == event.summary


def test_tool_action_sanitized_preview_and_full_arguments(isolated_tracing):
    event = tracing.action("VenueAgent", "search_venues", {"city": "Namur", "capacity": 150}, "c1")
    assert event.summary == "search_venues(city='Namur', capacity=150)"
    assert event.action == "ACTION"
    assert event.conversation_id == "c1"
    large = "x" * 20000
    secret_event = tracing.action("VenueAgent", "search", {
        "nested": {"api_key": "hidden-value"}, "query": "password=hidden-value", "large": large,
    })
    assert "hidden-value" not in json.dumps(secret_event.to_dict())
    assert "hidden-value" not in isolated_tracing[0].getvalue()
    assert secret_event.metadata["arguments"]["large"] == large
    assert len(secret_event.summary) < 1000


@pytest.mark.parametrize("result", ["é" * 2000, {"venues": ["Namur"] * 1000, "password": "hidden-value"},
                                     ["é" * 1000, {"access_token": "hidden-value"}], None, 42,
                                     {"message": "password=hidden-value"}])
def test_full_observations_for_listeners(result, isolated_tracing):
    events = []
    tracing.define_listener(events.append)
    event = tracing.observation("Agent", result, "c1")
    assert events == [event]
    assert event.action == "OBSERV"
    assert event.conversation_id == "c1"
    assert "hidden-value" not in json.dumps(event.to_dict())
    if isinstance(result, str):
        assert event.summary == event.metadata["result"] == result
    else:
        assert json.loads(event.summary) == event.metadata["result"]
    if len(event.summary) > 400:
        assert event.summary not in isolated_tracing[0].getvalue()
        assert "..." in isolated_tracing[0].getvalue()


def test_presentation_helpers_are_literal_and_do_not_publish(isolated_tracing):
    events = []
    tracing.define_listener(events.append)
    tracing.title("[bold]Planning[/bold]")
    tracing.panel("[red]Ready[/red]", title="[green]Result[/green]", color="green")
    output = isolated_tracing[0].getvalue()
    assert "[bold]Planning[/bold]" in output
    assert "[red]Ready[/red]" in output
    assert "[green]Result[/green]" in output
    assert events == []


def test_unavailable_error_stream_does_not_block_listeners(monkeypatch):
    events = []

    def fail(*args, **kwargs):
        raise OSError("Private failure details")

    tracing.define_listener(fail)
    tracing.define_listener(events.append)
    monkeypatch.setattr(tracing._error_console, "print", fail)
    event = tracing.info("Agent", "Ready")
    assert events == [event]
