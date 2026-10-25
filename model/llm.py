"""OpenAI-compatible chat, function tools, streaming, and in-memory accounting.

Only LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are read from configuration.
The root .env is loaded lazily, without overriding operating-system variables.
Call new_request() once per user turn, or pass request_id explicitly to each
call. Context-local request IDs group agents; call_id identifies one API call.

Only completed responses enter USAGE_JOURNAL. Failed or cancelled streams may
still incur provider charges that cannot be measured here. No prompts, tool
arguments, response text, or private reasoning are recorded in usage traces.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import re
from threading import Lock
import time
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError
from openai.types.chat import ChatCompletionMessage
from rich.table import Table
from rich.text import Text

from model import trace as tracing


DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 2048
RATE_LIMIT_DELAYS = (2.0, 5.0, 10.0, 20.0)
MAX_RETRY_DELAY = 60.0
USAGE_JOURNAL: list[dict] = []

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_client: OpenAI | None = None
_model: str | None = None
_client_lock = Lock()
_current_request: ContextVar[str | None] = ContextVar("llm_request_id", default=None)
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


class LLMError(Exception):
    """A configuration or API error raised by the LLM client."""


def get_client() -> OpenAI:
    """Lazily create and reuse the configured client, with SDK retries disabled.

    Configuration is fixed at first successful initialization. Request-specific
    timeout, output-token limit, and retry policy can be overridden on chat calls.
    """
    global _client, _model
    with _client_lock:
        if _client is None:
            try:
                load_dotenv(_ENV_PATH, override=False, interpolate=False)
                api_key = os.getenv("LLM_API_KEY", "").strip()
                base_url = os.getenv("LLM_BASE_URL", "").strip()
                model = os.getenv("LLM_MODEL", "").strip()
                if not api_key:
                    raise LLMError("Missing LLM_API_KEY configuration.")
                if not base_url or not model:
                    raise LLMError("LLM_BASE_URL and LLM_MODEL must be configured.")
                url = urlsplit(base_url)
                if (url.scheme not in {"https", "http"} or not url.hostname
                        or url.username is not None or url.password is not None
                        or url.query or url.fragment or any(char.isspace() for char in base_url)):
                    raise LLMError("LLM_BASE_URL must be an HTTP(S) URL without credentials or query parameters.")
                # Accessing port also validates malformed or out-of-range ports.
                _ = url.port
                if any(char.isspace() for char in model):
                    raise LLMError("LLM_MODEL must be a non-empty model identifier without whitespace.")
                client = OpenAI(api_key=api_key, base_url=base_url,
                                timeout=DEFAULT_TIMEOUT, max_retries=0)
            except LLMError:
                raise
            except Exception:
                raise LLMError("Unable to initialize the LLM client; check its configuration.") from None
            _model, _client = model, client
        return _client


def new_request() -> str:
    """Start a user request in this execution context and return its unique ID.

    Reuse the returned ID via request_id=... when dispatching work to threads.
    Without this helper or an explicit ID, each chat call starts its own request.
    """
    request_id = uuid4().hex
    _current_request.set(request_id)
    return request_id


def _get(value, name: str, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _safe_label(value) -> str | None:
    """Remove the configured key even if a provider echoes it in an identifier."""
    if not isinstance(value, str):
        return None
    key = getattr(_client, "api_key", None)
    if isinstance(key, str) and key:
        value = value.replace(key, "[REDACTED]")
    return value


@dataclass
class _Call:
    agent: str
    conversation_id: str | None
    request_id: str
    model: str
    call_id: str = field(default_factory=lambda: uuid4().hex)
    started: float = field(default_factory=time.perf_counter)
    attempts: int = 0

    def metadata(self) -> dict:
        return {"request_id": self.request_id, "call_id": self.call_id, "model": self.model}

    def emit(self, action: str, summary: str, **metadata):
        return tracing.trace(self.agent, action, summary, self.conversation_id,
                             self.metadata() | metadata)


def _finite_number(value) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _prepare(messages, tools, agent, temperature, conversation_id, request_id,
             timeout, max_tokens, retry_delays, max_retry_delay):
    if not isinstance(agent, str) or not agent.strip():
        raise LLMError("Agent must be a non-empty string.")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise LLMError("Conversation ID must be a string or None.")
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        raise LLMError("Request ID must be a non-empty string or None.")
    if not isinstance(messages, list) or not messages or not all(isinstance(item, dict) for item in messages):
        raise LLMError("Messages must be a non-empty list of dictionaries.")
    if tools is not None and (not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools)):
        raise LLMError("Tools must be a list of dictionaries or None.")
    if not _finite_number(temperature) or not 0 <= temperature <= 2:
        raise LLMError("Temperature must be a finite number between 0 and 2.")
    if not _finite_number(timeout) or timeout <= 0:
        raise LLMError("Timeout must be a positive finite number of seconds.")
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
        raise LLMError("Maximum output tokens must be a positive integer or None.")
    if not _finite_number(max_retry_delay) or max_retry_delay < 0:
        raise LLMError("Maximum retry delay must be a non-negative finite number.")
    if (not isinstance(retry_delays, (list, tuple))
            or any(not _finite_number(delay) or not 0 <= delay <= max_retry_delay for delay in retry_delays)):
        raise LLMError("Retry delays must be a list or tuple of non-negative bounded seconds.")
    try:
        client = get_client()
    except LLMError as exc:
        tracing.error(agent, str(exc), conversation_id)
        raise
    call = _Call(_safe_label(agent), _safe_label(conversation_id),
                 _safe_label(request_id or _current_request.get() or uuid4().hex), _safe_label(_model))
    params = {"model": _model, "messages": messages, "temperature": temperature, "timeout": timeout}
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if tools:
        params.update(tools=tools, tool_choice="auto")
    return client, params, call, tuple(retry_delays)


def _retry_after(exc: RateLimitError) -> float | None:
    headers = exc.response.headers
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            delay = float(raw) * scale
        except (TypeError, ValueError):
            if name != "retry-after":
                continue
            try:
                target = parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    continue
                delay = (target - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            return delay
    return None


def _create(client, params, call: _Call, retry_delays, max_retry_delay):
    """Retry only rejected rate-limited requests, before any stream is opened."""
    call.emit("LLM", "Calling the configured language model.")
    for attempt in range(len(retry_delays) + 1):
        call.attempts += 1
        try:
            return client.chat.completions.create(**params)
        except RateLimitError as exc:
            if attempt == len(retry_delays):
                raise
            delay = _retry_after(exc)
            if delay is None:
                delay = retry_delays[attempt]
            if delay > max_retry_delay:
                # Do not retry earlier than a valid provider Retry-After value.
                raise LLMError("Rate limit retry delay exceeds the configured wait limit.") from None
            call.emit("INFO", f"Rate limited; retrying in {delay:g} seconds.",
                      retry_number=attempt + 1, delay_seconds=delay)
            time.sleep(delay)


def _api_error(exc: Exception) -> LLMError:
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, APITimeoutError):
        return LLMError("The LLM request timed out.")
    if isinstance(exc, APIConnectionError):
        return LLMError("Unable to connect to the LLM provider.")
    messages = {
        400: "The LLM provider rejected the request (400); check messages, tools, and model parameters.",
        401: "LLM authentication failed (401); check LLM_API_KEY.",
        402: "The LLM provider requires payment or additional credits (402).",
        403: "LLM access was denied (403); check account and model permissions.",
        404: "The LLM model or endpoint was not found (404); check LLM_MODEL and LLM_BASE_URL.",
        429: "The LLM rate limit was reached (429); the bounded retry policy has stopped.",
    }
    return LLMError(messages.get(getattr(exc, "status_code", None),
                                 "An unexpected LLM provider error occurred."))


def _record_usage(call: _Call, response, usage) -> dict:
    entry = call.metadata() | {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "conversation_id": call.conversation_id, "agent": call.agent,
        "model": _safe_label(_get(response, "model")) or call.model,
        "provider_request_id": _safe_label(_get(response, "_request_id")),
        "provider_response_id": _safe_label(_get(response, "id")),
        "duration_seconds": time.perf_counter() - call.started,
        "attempts": call.attempts,
    }
    for name in _TOKEN_FIELDS:
        value = _get(usage, name)
        entry[name] = value if type(value) is int and value >= 0 else None
    cost = _get(usage, "cost")
    entry["cost"] = cost if _finite_number(cost) and cost >= 0 else None
    # Reuse trace.py's public serialization/sanitization contract for the journal.
    entry = tracing.TraceEvent(entry["timestamp"], call.conversation_id, call.agent,
                               "LLM", "", entry).to_dict()["metadata"]
    USAGE_JOURNAL.append(deepcopy(entry))
    token_text = lambda value: "unknown" if value is None else str(value)
    call.emit("TOKEN", f"Input: {token_text(entry['prompt_tokens'])} | "
              f"Output: {token_text(entry['completion_tokens'])}", **{
                  name: entry[name] for name in _TOKEN_FIELDS
              })
    cost_text = "API cost unavailable." if entry["cost"] is None else f"Actual API cost: ${entry['cost']:.6f}"
    call.emit("COST", cost_text, cost=entry["cost"])
    call.emit("INFO", f"Request completed in {entry['duration_seconds']:.2f} seconds.", **entry)
    return entry


def chat(
    messages: list[dict], tools: list[dict] | None = None, agent: str = "Unknown",
    temperature: float = 0.3, conversation_id: str | None = None, *,
    request_id: str | None = None, timeout: float = DEFAULT_TIMEOUT,
    max_tokens: int | None = DEFAULT_MAX_TOKENS,
    retry_delays: Sequence[float] = RATE_LIMIT_DELAYS,
    max_retry_delay: float = MAX_RETRY_DELAY,
) -> ChatCompletionMessage:
    """Return the assistant message (.content and .tool_calls), without executing tools.

    Duration includes rate-limit waits. Only 429 responses are retried, using the
    supplied delays; SDK retries are disabled. Unknown usage remains None.
    """
    client, params, call, delays = _prepare(
        messages, tools, agent, temperature, conversation_id, request_id,
        timeout, max_tokens, retry_delays, max_retry_delay,
    )
    try:
        result = _create(client, params, call, delays, max_retry_delay)
        if _get(result, "error") is not None:
            raise LLMError("The LLM provider returned an error response.")
        _record_usage(call, result, _get(result, "usage"))
        choices = _get(result, "choices")
        if not choices or _get(choices[0], "message") is None:
            raise LLMError("The LLM response contains no assistant message.")
        return choices[0].message
    except Exception as exc:
        failure = _api_error(exc)
        call.emit("ERROR", str(failure))
        raise failure from None


def stream_chat(
    messages: list[dict], tools: list[dict] | None = None, agent: str = "Unknown",
    temperature: float = 0.3, conversation_id: str | None = None, *,
    request_id: str | None = None, timeout: float = DEFAULT_TIMEOUT,
    max_tokens: int | None = DEFAULT_MAX_TOKENS,
    retry_delays: Sequence[float] = RATE_LIMIT_DELAYS,
    max_retry_delay: float = MAX_RETRY_DELAY, include_usage: bool = True,
) -> Iterator[dict]:
    """Yield dictionaries with request_id, call_id, and one of these event shapes:

    * {type: 'text_delta', text: str}
    * {type: 'refusal_delta', text: str}
    * {type: 'tool_call_delta', index: int, id: str|None,
       name: str|None, arguments: str|None}
    * {type: 'completed', message: ChatCompletionMessage, usage: dict,
       finish_reason: str}

    No fragments are printed or traced. Tool deltas are assembled by index.
    The final event follows stream exhaustion, including the final usage frame.
    OpenRouter and OpenAI final usage frame shapes are both supported.
    Set include_usage=False for providers rejecting stream_options.

    Use contextlib.closing(stream_chat(...)) if stopping iteration early; close()
    releases the connection. Interrupted streams raise LLMError (or propagate
    cancellation), emit no completed event, and do not enter the usage journal.
    No retry is attempted once a stream has been opened.
    """
    client, params, call, delays = _prepare(
        messages, tools, agent, temperature, conversation_id, request_id,
        timeout, max_tokens, retry_delays, max_retry_delay,
    )
    params["stream"] = True
    if include_usage:
        params["stream_options"] = {"include_usage": True}
    stream = None
    completed = False
    content, refusal, tool_calls = [], [], {}
    finish_reason, usage, last_chunk = None, None, None
    provider_id, provider_model = None, None
    identifiers = {"request_id": call.request_id, "call_id": call.call_id}
    try:
        stream = _create(client, params, call, delays, max_retry_delay)
        for chunk in stream:
            last_chunk = chunk
            provider_id = _get(chunk, "id") or provider_id
            provider_model = _get(chunk, "model") or provider_model
            if _get(chunk, "error") is not None:
                raise LLMError("The LLM provider reported a streaming error.")
            if _get(chunk, "usage") is not None:
                usage = chunk.usage
            for choice in _get(chunk, "choices", []) or []:
                if choice.index != 0:
                    continue
                if choice.finish_reason == "error":
                    raise LLMError("The LLM provider reported a streaming error.")
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta
                for name, parts, event_type in (("content", content, "text_delta"),
                                                 ("refusal", refusal, "refusal_delta")):
                    text = _get(delta, name)
                    if text is not None:
                        parts.append(text)
                        if text:
                            yield identifiers | {"type": event_type, "text": text}
                for update in _get(delta, "tool_calls", []) or []:
                    if type(update.index) is not int or update.index < 0:
                        raise LLMError("The LLM stream contains an invalid tool-call index.")
                    tool = tool_calls.setdefault(update.index, {
                        "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                    })
                    if _get(update, "type") not in {None, "function"}:
                        raise LLMError("Only function tool calls are supported.")
                    function = _get(update, "function")
                    tool_id, name, arguments = _get(update, "id"), _get(function, "name"), _get(function, "arguments")
                    tool["id"] += tool_id or ""
                    tool["function"]["name"] += name or ""
                    tool["function"]["arguments"] += arguments or ""
                    yield identifiers | {"type": "tool_call_delta", "index": update.index,
                                         "id": tool_id, "name": name, "arguments": arguments}
        if last_chunk is None or finish_reason is None:
            raise LLMError("The LLM stream ended before a completion marker was received.")
        ordered_tools = [tool_calls[index] for index in sorted(tool_calls)]
        if any(not tool["id"] or not tool["function"]["name"] for tool in ordered_tools):
            raise LLMError("The LLM stream contains an incomplete tool call.")
        message = ChatCompletionMessage(
            role="assistant", content="".join(content) if content else None,
            refusal="".join(refusal) if refusal else None, tool_calls=ordered_tools or None,
        )
        response = {"id": provider_id, "model": provider_model,
                    "_request_id": _get(_get(stream, "response"), "headers", {}).get("x-request-id")}
        entry = _record_usage(call, response, usage)
        completed = True
    except (GeneratorExit, KeyboardInterrupt):
        call.emit("INFO", "Streaming request cancelled; final usage is unavailable.")
        raise
    except Exception as exc:
        failure = _api_error(exc)
        call.emit("ERROR", str(failure))
        raise failure from None
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                call.emit("INFO", "Unable to close the LLM stream cleanly.")
    if completed:
        yield identifiers | {"type": "completed", "message": message,
                             "usage": entry, "finish_reason": finish_reason}


def tool_schema(name: str, description: str, properties: dict,
                required: list[str] | None = None) -> dict:
    """Build a function tool definition; None requires all properties, [] none."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise LLMError("Tool names must contain 1 to 64 letters, digits, underscores, or hyphens.")
    if not isinstance(description, str) or not isinstance(properties, dict):
        raise LLMError("Tool description must be a string and properties a dictionary.")
    if not all(isinstance(key, str) and isinstance(value, dict) for key, value in properties.items()):
        raise LLMError("Tool properties must map string names to schema dictionaries.")
    if required is None:
        required = list(properties)
    if (not isinstance(required, list) or not all(isinstance(key, str) and key in properties for key in required)
            or len(set(required)) != len(required)):
        raise LLMError("Required properties must be unique names defined in properties.")
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": deepcopy(properties), "required": list(required)},
    }}


def _reject_json_constant(value):
    raise ValueError("Non-finite JSON numbers are not supported.")


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds the supported numeric range.")
    return number


def parse_arguments(tool_call) -> dict:
    """Parse JSON object arguments; blank arguments mean {}, malformed data raises LLMError."""
    function = _get(tool_call, "function")
    if function is None:
        raise LLMError("Tool call has no function definition.")
    arguments = _get(function, "arguments")
    if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
        return {}
    if not isinstance(arguments, str):
        raise LLMError("Tool arguments must be a JSON string.")
    try:
        result = json.loads(arguments, parse_constant=_reject_json_constant, parse_float=_json_float)
    except (ValueError, RecursionError):
        raise LLMError("Tool arguments are not valid JSON.") from None
    if not isinstance(result, dict):
        raise LLMError("Tool arguments must decode to a JSON object.")
    return result


def _tool_call_dict(tool_call) -> dict:
    function = _get(tool_call, "function")
    tool_id, name, arguments = _get(tool_call, "id"), _get(function, "name"), _get(function, "arguments")
    if (_get(tool_call, "type", "function") != "function"
            or not isinstance(tool_id, str) or not tool_id
            or not isinstance(name, str) or not name or not isinstance(arguments, str)):
        raise LLMError("Expected a complete function tool call with an ID, name, and argument string.")
    return {"id": tool_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def assistant_message(message) -> dict:
    """Convert an assistant response to history, preserving null content and tools.

    Only standard conversation fields are copied; provider reasoning extensions
    and other internal response metadata are deliberately not persisted.
    """
    if _get(message, "role") != "assistant":
        raise LLMError("Expected an assistant message.")
    result = {"role": _get(message, "role"), "content": _get(message, "content")}
    if _get(message, "tool_calls"):
        result["tool_calls"] = [_tool_call_dict(tool) for tool in _get(message, "tool_calls")]
    if _get(message, "refusal") is not None:
        result["refusal"] = _get(message, "refusal")
    return result


def tool_message(tool_call, result) -> dict:
    """Build a Unicode-capable tool result message; this function executes no tool."""
    tool = _tool_call_dict(tool_call)
    if not isinstance(result, str):
        try:
            result = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise LLMError("Tool result must be a string or JSON-serializable value.") from None
    return {"role": "tool", "tool_call_id": tool["id"], "name": tool["function"]["name"], "content": result}


def _aggregate(entries: list[dict]) -> dict:
    summary = {"calls": len(entries)}
    for name in (*_TOKEN_FIELDS, "cost"):
        known = [entry[name] for entry in entries if entry[name] is not None]
        missing = len(entries) - len(known)
        key = "known_cost" if name == "cost" else name
        summary[key] = (math.fsum(known) if name == "cost" else sum(known)) if known or not entries else None
        summary[f"missing_{name}_calls"] = missing
    summary["cost_complete"] = summary["missing_cost_calls"] == 0
    summary["tokens_complete"] = all(summary[f"missing_{name}_calls"] == 0 for name in _TOKEN_FIELDS)
    return summary


def usage_summary(conversation_id: str | None = None, *, request_id: str | None = None) -> dict:
    """Aggregate completed calls, optionally filtering by conversation and/or request.

    None means no filter. Token fields and known_cost sum reported values only;
    all-unknown fields are None. missing_*_calls counts identify partial totals.
    Empty selections have zero calls and complete zero totals.
    """
    entries = [entry for entry in tuple(USAGE_JOURNAL)
               if (conversation_id is None or entry["conversation_id"] == conversation_id)
               and (request_id is None or entry["request_id"] == request_id)]
    agents = {}
    for entry in entries:
        agents.setdefault(entry["agent"], []).append(entry)
    return {"agents": {agent: _aggregate(items) for agent, items in agents.items()},
            "total": _aggregate(entries)}


def display_usage_summary(conversation_id: str | None = None, *, request_id: str | None = None) -> None:
    """Render optional usage totals with the shared console; mark missing/partial data."""
    summary = usage_summary(conversation_id, request_id=request_id)
    table = Table(title="LLM usage (completed calls)")
    for name in ("Agent", "API calls", "Input tokens", "Output tokens", "Total tokens", "Known cost in USD"):
        table.add_column(name, justify="left" if name == "Agent" else "right")
    for agent, stats in [*summary["agents"].items(), ("TOTAL", summary["total"])]:
        values = [Text(agent), str(stats["calls"])]
        for name in (*_TOKEN_FIELDS, "cost"):
            value = stats["known_cost" if name == "cost" else name]
            text = "unknown" if value is None else (f"${value:.6f}" if name == "cost" else str(value))
            if value is not None and stats[f"missing_{name}_calls"]:
                text += " (partial)"
            values.append(text)
        table.add_row(*values, style="bold" if agent == "TOTAL" else None)
    # No public console-state accessor exists; preserve the tracing toggle.
    if getattr(tracing, "_console_enabled", True):
        tracing.console.print(table)
