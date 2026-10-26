"""Explicit trace listeners, filtering, scoped deletion, and safe failures."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json

import pytest
from rich.console import Console

from dao import TraceDAO, ConversationDAO
from dao.json_storage import StorageFormatError
from model import trace


def event(conversation_id="conv_1", agent="A", timestamp="2026-10-26T10:00:00+00:00", **kwargs):
    return trace.TraceEvent(timestamp, conversation_id, agent, "INFO", "Recorded", kwargs)


def test_save_filter_restart_and_defensive_copies(storage_dir):
    dao = TraceDAO(storage_dir)
    records = [event("conv_1", "A"), event("conv_1", "B"), event("conv_2", "A"), event(None, "A")]
    for record in records:
        dao.save_trace(record)
    reopened = TraceDAO(storage_dir)
    assert len(reopened.get_traces()) == 4
    assert len(reopened.get_traces("conv_1")) == 2
    assert len(reopened.get_traces(agent="A")) == 3
    assert len(reopened.get_traces("conv_1", "B")) == 1
    result = reopened.get_traces()
    result[0]["metadata"]["changed"] = True
    assert "changed" not in reopened.get_traces()[0]["metadata"]
    assert not (storage_dir / "conv.json").exists()


def test_explicit_registration_is_idempotent_per_log_path(storage_dir):
    first, second = TraceDAO(storage_dir), TraceDAO(storage_dir)
    assert trace._listeners == [] and not storage_dir.exists()
    first.register_listener()
    first.register_listener()
    second.register_listener()
    assert len(trace._listeners) == 1
    emitted = trace.info("Agent", "Ready", "conv_1")
    assert first.get_traces() == [emitted.to_dict()]
    second.unregister_listener()
    first.unregister_listener()
    trace.info("Agent", "Not persisted", "conv_1")
    assert len(first.get_traces()) == 1


def test_multiple_log_destinations_and_concurrent_registration(storage_dir, tmp_path):
    first = TraceDAO(storage_dir)
    second = TraceDAO(tmp_path / "other_logs")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: TraceDAO(storage_dir).register_listener(), range(8)))
    second.register_listener()
    trace.info("Agent", "Ready")
    assert len(trace._listeners) == 2
    assert len(first.get_traces()) == len(second.get_traces()) == 1


def test_clear_is_scoped_and_full_clear_is_explicit(storage_dir):
    dao = TraceDAO(storage_dir)
    for identifier in ["conv_1", "conv_2", None]:
        dao.save_trace(event(identifier))
    with pytest.raises(ValueError, match="clear_all_traces"):
        dao.clear_traces()
    assert len(dao.get_traces()) == 3
    dao.clear_traces("conv_1")
    assert {record["conversation_id"] for record in dao.get_traces()} == {"conv_2", None}
    dao.clear_all_traces()
    assert dao.get_traces() == []


def test_normalizes_timestamps_to_utc_and_sorts(storage_dir):
    dao = TraceDAO(storage_dir)
    dao.save_trace(event(timestamp="2026-10-26T10:00:00Z"))
    dao.save_trace(event(timestamp="2026-10-26T10:30:00+02:00"))
    assert [item["timestamp"] for item in dao.get_traces()] == [
        "2026-10-26T08:30:00+00:00", "2026-10-26T10:00:00+00:00",
    ]


def test_trace_sanitization_drops_internal_fields_but_preserves_usage(storage_dir):
    original = event(api_key="private-key", headers={"Authorization": "private-header"},
                     environment={"LLM_API_KEY": "private-env"}, reasoning="private-thought",
                     nested=[{"reasoning_details": "private-details", "password": "private-password"}],
                     prompt_tokens=10, completion_tokens=2, total_tokens=12, reasoning_tokens=1, cost=0.001)
    TraceDAO(storage_dir).save_trace(original)
    stored = TraceDAO(storage_dir).get_traces()[0]
    assert stored["metadata"]["api_key"] == "[REDACTED]"
    assert stored["metadata"]["total_tokens"] == 12 and stored["metadata"]["reasoning_tokens"] == 1
    assert stored["metadata"]["cost"] == 0.001
    raw = (storage_dir / "logs.json").read_text(encoding="utf-8")
    for secret in ["private-key", "private-header", "private-env", "private-thought", "private-details", "private-password"]:
        assert secret not in raw
    assert original.metadata["api_key"] == "private-key"


def test_listener_failure_does_not_recurse_or_hide_other_listeners(monkeypatch, storage_dir):
    dao = TraceDAO(storage_dir)
    dao.register_listener()
    path = storage_dir / "logs.json"
    path.write_text("malformed-private-file", encoding="utf-8")
    output = StringIO()
    monkeypatch.setattr(trace, "_error_console", Console(file=output))
    received = []
    trace.define_listener(received.append)
    emitted = trace.info("Agent", "Ready")
    assert received == [emitted]
    assert output.getvalue().count("Trace listener failed") == 1
    assert "malformed-private-file" not in output.getvalue()
    assert path.read_text(encoding="utf-8") == "malformed-private-file"
    with pytest.raises(StorageFormatError):
        dao.save_trace(emitted)


def test_register_preflights_corrupt_storage(storage_dir):
    storage_dir.mkdir()
    (storage_dir / "logs.json").write_text("", encoding="utf-8")
    with pytest.raises(StorageFormatError):
        TraceDAO(storage_dir).register_listener()
    assert trace._listeners == []


def test_concurrent_trace_writes_do_not_lose_events(storage_dir):
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda index: TraceDAO(storage_dir).save_trace(event(sequence=index)), range(24)))
    records = TraceDAO(storage_dir).get_traces()
    assert len(records) == 24
    assert {item["metadata"]["sequence"] for item in records} == set(range(24))


def test_trace_events_never_become_visible_chat_messages(storage_dir):
    conversations, traces = ConversationDAO(storage_dir), TraceDAO(storage_dir)
    identifier = conversations.create_conversation()
    traces.register_listener()
    trace.trace("Agent", "ASK", "Internal task", identifier)
    trace.response("Agent", "Internal response", identifier)
    assert len(traces.get_traces(identifier)) == 2
    assert conversations.get_messages(identifier) == []


@pytest.mark.parametrize("changes", [{"timestamp": "not a timestamp"}, {"timestamp": "2026-10-26T10:00:00"},
                                      {"conversation_id": "../unsafe"}, {"action": "UNKNOWN"}, {"agent": ""}])
def test_invalid_trace_events_rejected(changes, storage_dir):
    record = event()
    for key, value in changes.items():
        setattr(record, key, value)
    with pytest.raises(ValueError):
        TraceDAO(storage_dir).save_trace(record)
    assert not storage_dir.exists()


def test_save_trace_requires_trace_event(storage_dir):
    with pytest.raises(TypeError):
        TraceDAO(storage_dir).save_trace({"summary": "Not a TraceEvent"})
