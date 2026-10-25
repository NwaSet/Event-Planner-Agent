"""Structured tracing with optional Rich presentation and synchronous listeners.

Persistence belongs to consumers. Text redaction is best effort: callers must
avoid credentials, raw headers, sensitive exceptions, and private LLM reasoning.
Listeners should treat shared events as read-only and return promptly.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
import json
import math
from pathlib import PurePath
import re
import reprlib
from uuid import UUID

from rich.console import Console
from rich.panel import Panel
from rich.text import Text


COLORS_TYPES = {
    "ASK": "bold white",
    "THINK": "yellow",
    "ACTION": "cyan",
    "OBSERV": "green",
    "RESPONSE": "bold magenta",
    "LLM": "dim",
    "HUMAN": "bold red",
    "INFO": "blue",
    "ERROR": "bold red",
    "TOOL": "bright_cyan",
    "TOKEN": "magenta",
    "COST": "bright_green",
}

AGENTS_PALETTE = [
    "bright_blue", "bright_magenta", "orange1", "bright_cyan",
    "bright_green", "deep_pink2", "gold1", "medium_purple1",
]

console = Console()
_error_console = Console(stderr=True)
_console_enabled = True
_colors_agents: dict[str, str] = {}
_agent_width = 12
_REDACTED = "[REDACTED]"
_SENSITIVE_NAMES = (
    "api_key", "authorization", "password", "passwd", "secret",
    "access_token", "refresh_token", "id_token", "auth_token",
    "client_secret", "credential", "cookie",
)
_SENSITIVE_KEY_PARTS = tuple(name.replace("_", "") for name in _SENSITIVE_NAMES)
_TEXT_NAMES = "|".join(name.replace("_", r"[\s_-]*") for name in _SENSITIVE_NAMES)
_ASSIGNMENT = re.compile(
    rf"(?P<label>\b(?:{_TEXT_NAMES}|token)\b[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    r"(?:Bearer|Basic)\s+[^\s,;}\"']+|\[REDACTED\]|[^\s,;}\]\"']+)",
    re.IGNORECASE,
)
_AUTH_SCHEME = re.compile(r"\b(Bearer|Basic)\s+[^\s,;\"'}]+", re.IGNORECASE)
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_preview = reprlib.Repr()
_preview.maxstring = 100
_preview.maxother = 100
_preview.maxdict = 6
_preview.maxlist = 6
_preview.maxtuple = 6
_preview.maxlevel = 3


def _sanitize_text(text: str) -> str:
    """Redact recognizable credentials; arbitrary text cannot be fully sanitized."""
    def replace_assignment(match: re.Match) -> str:
        quote = match["value"][0] if match["value"][0] in "\"'" else ""
        return match["label"] + quote + _REDACTED + quote

    text = _ASSIGNMENT.sub(replace_assignment, text)
    text = _AUTH_SCHEME.sub(lambda match: f"{match[1]} {_REDACTED}", text)
    return _API_KEY.sub(_REDACTED, text)


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return (
        normalized in {"token", "headers", "requestheaders", "responseheaders"}
        or any(part in normalized for part in _SENSITIVE_KEY_PARTS)
    )


def _normalize(value: object, active: set[int] | None = None) -> object:
    """Copy to sanitized JSON data. Dates use ISO format; decimals use strings.

    Bytes use hex. Cycles and unsupported objects use markers, never unsafe repr.
    """
    if isinstance(value, Enum):
        return _normalize(value.value, active)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, PurePath, UUID)):
        return _sanitize_text(str(value))
    if isinstance(value, (bytes, bytearray)):
        return {"encoding": "hex", "data": value.hex()}
    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        return "[CIRCULAR]"
    active.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            value = {field.name: getattr(value, field.name) for field in fields(value)}
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    key = json.dumps(_normalize(key, active), ensure_ascii=False)
                result[_sanitize_text(key)] = (
                    _REDACTED if _sensitive_key(key) else _normalize(item, active)
                )
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            return [_normalize(item, active) for item in value]
        return "[UNSUPPORTED OBJECT]"
    finally:
        active.remove(identity)


@dataclass
class TraceEvent:
    """Stable event payload for terminal, UI, and persistence consumers."""

    timestamp: str
    conversation_id: str | None
    agent: str
    action: str
    summary: str
    metadata: dict

    def to_dict(self) -> dict:
        """Return an independent, sanitized, strictly JSON-compatible snapshot."""
        return _normalize({field.name: getattr(self, field.name) for field in fields(self)})


_listeners: list[Callable[[TraceEvent], None]] = []


def define_listener(callback: Callable[[TraceEvent], None]) -> None:
    """Subscribe once; callbacks run synchronously in registration order."""
    if not callable(callback):
        raise TypeError("Listener must be callable.")
    if callback not in _listeners:
        _listeners.append(callback)


def remove_listener(callback: Callable[[TraceEvent], None]) -> None:
    """Unsubscribe a callback; an absent callback is harmless."""
    if callback in _listeners:
        _listeners.remove(callback)


def clear_listeners() -> None:
    """Remove all registered listeners."""
    _listeners.clear()


def agent_color(agent: str) -> str:
    """Assign palette colors in first-appearance order for this process."""
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("Agent name must be a non-empty string.")
    if agent not in _colors_agents:
        _colors_agents[agent] = AGENTS_PALETTE[len(_colors_agents) % len(AGENTS_PALETTE)]
    return _colors_agents[agent]


def reduce_text(text: str, max_length: int = 400) -> str:
    """Bound presentation length, including the ellipsis."""
    if max_length < 0:
        raise ValueError("Maximum length must not be negative.")
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return "." * max_length
    return text[:max_length - 3] + "..."


def enable_console() -> None:
    """Enable terminal presentation without changing subscriptions."""
    global _console_enabled
    _console_enabled = True


def disable_console() -> None:
    """Disable presentation; listener failure diagnostics still use stderr."""
    global _console_enabled
    _console_enabled = False


def _render(event: TraceEvent) -> None:
    global _agent_width
    name = reduce_text(" ".join(event.agent.split()), 40)
    _agent_width = max(_agent_width, len(name))
    line = Text(name.ljust(_agent_width), style=agent_color(event.agent))
    line.append("  ")
    line.append(f"[{event.action}]".ljust(10), style=COLORS_TYPES[event.action])
    line.append(" " + reduce_text(event.summary))
    console.print(line, highlight=False)


def trace(
    agent: str,
    action: str,
    summary: str,
    conversation_id: str | None = None,
    metadata: dict | None = None,
) -> TraceEvent:
    """Create, display, and publish an event with an aware UTC timestamp.

    Summaries describe activity, never private LLM reasoning. Callers must avoid
    secrets in free-form text. Subscription changes apply to the next event.
    """
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("Agent name must be a non-empty string.")
    if not isinstance(action, str) or action not in COLORS_TYPES:
        raise ValueError("Unsupported trace event type.")
    if not isinstance(summary, str):
        raise TypeError("Summary must be a string.")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise TypeError("Conversation ID must be a string or None.")
    if metadata is not None and not isinstance(metadata, dict):
        raise TypeError("Metadata must be a dictionary or None.")
    event = TraceEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        conversation_id=_sanitize_text(conversation_id) if conversation_id is not None else None,
        agent=_sanitize_text(agent),
        action=action,
        summary=_sanitize_text(summary),
        metadata=_normalize(metadata if metadata is not None else {}),
    )
    agent_color(event.agent)
    if _console_enabled:
        _render(event)
    for callback in tuple(_listeners):
        try:
            callback(event)
        except Exception:
            # Never include callback repr, exception text, or a new trace event.
            try:
                _error_console.print(Text("Trace listener failed; event delivery continued."))
            except Exception:
                # An unavailable error stream must not block other listeners.
                pass
    return event


def think(agent: str, summary: str, conversation_id: str | None = None) -> TraceEvent:
    """Record a task description or intended action, never private reasoning."""
    return trace(agent, "THINK", summary, conversation_id)


def action(
    agent: str, tool_name: str, arguments: dict, conversation_id: str | None = None,
) -> TraceEvent:
    """Record complete sanitized tool data with a bounded argument preview."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError("Tool name must be a non-empty string.")
    if not isinstance(arguments, dict):
        raise TypeError("Tool arguments must be a dictionary.")
    safe_arguments = _normalize(arguments)
    preview = []
    for index, (key, value) in enumerate(safe_arguments.items()):
        if index == 6:
            preview.append("...")
            break
        preview.append(f"{reduce_text(key, 60)}={_preview.repr(value)}")
    safe_tool_name = _sanitize_text(tool_name)
    summary = f"{reduce_text(safe_tool_name, 100)}({', '.join(preview)})"
    return trace(agent, "ACTION", summary, conversation_id,
                 {"tool_name": safe_tool_name, "arguments": safe_arguments})


def observation(agent: str, result: object, conversation_id: str | None = None) -> TraceEvent:
    """Preserve complete sanitized results; only terminal output is truncated."""
    safe_result = _normalize(result)
    summary = safe_result if isinstance(safe_result, str) else json.dumps(
        safe_result, ensure_ascii=False, allow_nan=False,
    )
    return trace(agent, "OBSERV", summary, conversation_id, {"result": safe_result})


def response(agent: str, text: str, conversation_id: str | None = None) -> TraceEvent:
    """Record an agent response."""
    return trace(agent, "RESPONSE", text, conversation_id)


def human(agent: str, message: str, conversation_id: str | None = None) -> TraceEvent:
    """Record human intervention without collecting or waiting for input."""
    return trace(agent, "HUMAN", message, conversation_id)


def error(agent: str, message: str, conversation_id: str | None = None) -> TraceEvent:
    """Record a safe error description, without automatic exception details."""
    return trace(agent, "ERROR", message, conversation_id)


def info(agent: str, message: str, conversation_id: str | None = None) -> TraceEvent:
    """Record general application information."""
    return trace(agent, "INFO", message, conversation_id)


def title(text: str) -> None:
    """Display a literal section title without publishing an event."""
    if _console_enabled:
        console.rule(Text(_sanitize_text(text)))


def panel(text: str, title: str = "", color: str = "white") -> None:
    """Display literal panel content without publishing an event."""
    if _console_enabled:
        console.print(Panel(Text(_sanitize_text(text)), title=Text(_sanitize_text(title)),
                            border_style=color))
