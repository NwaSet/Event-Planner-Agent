"""Technical trace storage in logs.json, independent of visible conversations.

Registration is explicit. register_listener() preflights storage and registers
at most one callback per canonical log path, even across DAO instances. Calling
unregister_listener() on any instance for that path removes that callback.
The tracing module catches listener failures; direct save_trace() calls raise.
No persistence failure emits a new trace, preventing recursive logging failures.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from dao.conversation_dao import _utc
from dao.json_storage import (JSONStorage, parse_timestamp, sanitize_payload,
                              validate_agent_name, validate_identifier)
from model import trace as tracing


_REGISTRATION_LOCK = Lock()


def _validate_events(data: dict) -> None:
    for event in data["events"]:
        if not isinstance(event, dict):
            raise ValueError("Stored trace events must be dictionaries.")
        _utc(event.get("timestamp"))
        if "conversation_id" not in event:
            raise ValueError("Trace events require a conversation ID field, possibly None.")
        if event["conversation_id"] is not None:
            validate_identifier(event["conversation_id"], "Conversation ID")
        validate_agent_name(event.get("agent"))
        if not isinstance(event.get("action"), str) or event["action"] not in tracing.COLORS_TYPES:
            raise ValueError("Unsupported trace event type.")
        if not isinstance(event.get("summary"), str) or not isinstance(event.get("metadata"), dict):
            raise ValueError("Trace summary and metadata have invalid structures.")


@dataclass(frozen=True)
class _Listener:
    path: Path
    save: Callable = field(compare=False, repr=False)

    def __call__(self, event):
        self.save(event)


class TraceDAO:
    def __init__(self, directory: str | Path | None = None):
        self._storage = JSONStorage("logs.json", directory, validator=_validate_events)
        self._listener = _Listener(self._storage.path, self.save_trace)

    def save_trace(self, event: tracing.TraceEvent) -> None:
        """Save a sanitized TraceEvent, normalized to a UTC timestamp."""
        if not isinstance(event, tracing.TraceEvent):
            raise TypeError("save_trace expects a TraceEvent.")
        payload = sanitize_payload(event.to_dict())
        payload["timestamp"] = parse_timestamp(payload["timestamp"]).isoformat()
        _validate_events({"events": [payload]})
        self._storage.update(lambda data: data["events"].append(payload))

    def get_traces(self, conversation_id: str | None = None, agent: str | None = None) -> list[dict]:
        """Return traces chronologically. None means no filter for that field."""
        if conversation_id is not None:
            validate_identifier(conversation_id, "Conversation ID")
        if agent is not None:
            validate_agent_name(agent)
        events = [event for event in self._storage.read()["events"]
                  if (conversation_id is None or event["conversation_id"] == conversation_id)
                  and (agent is None or event["agent"] == agent)]
        return sorted(events, key=lambda event: parse_timestamp(event["timestamp"]))

    def clear_traces(self, conversation_id: str | None = None) -> None:
        """Delete one conversation's traces; None is rejected to avoid an implicit full clear."""
        if conversation_id is None:
            raise ValueError("Specify a conversation ID, or explicitly call clear_all_traces().")
        validate_identifier(conversation_id, "Conversation ID")

        def clear(data):
            data["events"] = [event for event in data["events"] if event["conversation_id"] != conversation_id]

        self._storage.update(clear)

    def clear_all_traces(self) -> None:
        """Explicitly remove every technical trace, including unscoped events."""
        self._storage.update(lambda data: data["events"].clear())

    def register_listener(self) -> None:
        """Explicitly enable persistence once per log path; surface startup errors now."""
        self._storage.read()
        with _REGISTRATION_LOCK:
            tracing.define_listener(self._listener)

    def unregister_listener(self) -> None:
        """Disconnect the listener for this log path without deleting stored traces."""
        with _REGISTRATION_LOCK:
            tracing.remove_listener(self._listener)
