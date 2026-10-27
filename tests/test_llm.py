"""Offline LLM contracts, security checks, and real-SDK tests with mock transports."""

from contextlib import closing
from contextvars import Context
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import httpx2
import openai
from openai.types.chat import ChatCompletionMessage
import pytest
from rich.console import Console

from model import llm
from model import trace as tracing


MESSAGES = [{"role": "user", "content": "Organise une fête à Namur."}]
SECRET = "test-private-credential-never-log"


def completion(*, usage=None, content="Bonjour", tools=None, model="provider/model", response_id="gen-123"):
    return NS(id=response_id, _request_id="http-request-123", model=model, usage=usage,
              choices=[NS(message=ChatCompletionMessage(role="assistant", content=content, tool_calls=tools))])


def function_call(arguments='{"city":"Namur"}', tool_id="call-1", name="search_venues"):
    return {"id": tool_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def chunk(content=None, *, tools=None, finish=None, usage=None, refusal=None, choices=True, **extra):
    return NS(id="gen-stream", model="stream-model", usage=usage,
              choices=[NS(index=0, finish_reason=finish,
                          delta=NS(content=content, refusal=refusal, tool_calls=tools))] if choices else [], **extra)


def delta(index=0, tool_id=None, name=None, arguments=None):
    return NS(index=index, id=tool_id, type="function", function=NS(name=name, arguments=arguments))


class FakeStream:
    def __init__(self, items):
        self.items = items
        self.closed = False
        self.response = NS(headers={"x-request-id": "http-stream-request"})

    def __iter__(self):
        for item in self.items:
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self):
        self.closed = True


def api_error(error_type, status, headers=None):
    request = httpx2.Request("POST", "https://provider.invalid/chat/completions",
                             headers={"Authorization": f"Bearer {SECRET}"})
    response = httpx2.Response(status, request=request, headers=headers)
    return error_type(f"Provider echoed {SECRET}", response=response, body={"secret": SECRET})


@pytest.fixture(autouse=True)
def isolated_llm(monkeypatch):
    # Never load the real .env or permit a real SDK request in unit tests.
    monkeypatch.setattr(llm, "load_dotenv", Mock())
    for name, value in {"LLM_API_KEY": SECRET, "LLM_BASE_URL": "https://openrouter.ai/api/v1",
                        "LLM_MODEL": "configured/model"}.items():
        monkeypatch.setenv(name, value)
    client = NS(api_key=SECRET, chat=NS(completions=NS(create=Mock(return_value=completion()))))
    factory = Mock(return_value=client)
    monkeypatch.setattr(llm, "OpenAI", factory)
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "_model", None)
    monkeypatch.setattr(llm, "USAGE_JOURNAL", [])
    request_token = llm._current_request.set(None)
    monkeypatch.setattr(llm, "time", NS(perf_counter=llm.time.perf_counter, sleep=Mock()))
    monkeypatch.setattr(tracing, "_listeners", [])
    monkeypatch.setattr(tracing, "_console_enabled", False)
    monkeypatch.setattr(tracing, "_colors_agents", {})
    events = []
    tracing.define_listener(events.append)
    yield NS(client=client, create=client.chat.completions.create, factory=factory, events=events)
    llm._current_request.reset(request_token)


def test_lazy_reusable_client_and_root_env(isolated_llm):
    assert llm._client is None
    first = llm.get_client()
    assert llm.get_client() is first
    isolated_llm.factory.assert_called_once_with(api_key=SECRET, base_url="https://openrouter.ai/api/v1",
                                                timeout=60.0, max_retries=0)
    llm.load_dotenv.assert_called_once_with(llm._ENV_PATH, override=False, interpolate=False)
    assert llm._ENV_PATH == Path(llm.__file__).resolve().parents[1] / ".env"


def test_environment_takes_priority_and_dotenv_fills_missing(monkeypatch, isolated_llm):
    from dotenv import load_dotenv as real_load_dotenv

    monkeypatch.delenv("LLM_MODEL")

    def load(path, **kwargs):
        return real_load_dotenv(stream=StringIO(
            "LLM_API_KEY=ignored-file-key\nLLM_BASE_URL=https://file.invalid/v1\nLLM_MODEL=file-model\n"
        ), **kwargs)

    monkeypatch.setattr(llm, "load_dotenv", load)
    llm.chat(MESSAGES)
    assert isolated_llm.factory.call_args.kwargs["api_key"] == SECRET
    assert isolated_llm.factory.call_args.kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert isolated_llm.create.call_args.kwargs["model"] == "file-model"


@pytest.mark.parametrize("name", ["LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL"])
def test_missing_configuration(name, monkeypatch, isolated_llm):
    monkeypatch.delenv(name)
    with pytest.raises(llm.LLMError, match="configur"):
        llm.chat(MESSAGES)
    isolated_llm.factory.assert_not_called()
    assert isolated_llm.events[-1].action == "ERROR"
    assert llm.USAGE_JOURNAL == []


@pytest.mark.parametrize("url", ["", "ftp://provider.invalid", "not-a-url", "https://",
                                  "https://user:password@host.invalid", "https://host.invalid?key=hidden",
                                  "https://host.invalid/#hidden", "https://host.invalid:invalid", "https://bad host"])
def test_invalid_base_url(url, monkeypatch, isolated_llm):
    monkeypatch.setenv("LLM_BASE_URL", url)
    with pytest.raises(llm.LLMError):
        llm.get_client()
    isolated_llm.factory.assert_not_called()


def test_initialization_failure_hides_details(isolated_llm):
    isolated_llm.factory.side_effect = RuntimeError(SECRET)
    with pytest.raises(llm.LLMError) as exc:
        llm.get_client()
    assert SECRET not in str(exc.value)
    assert exc.value.__suppress_context__
    assert llm._client is None


def test_chat_parameters_return_message_and_traces(isolated_llm):
    usage = NS(prompt_tokens=1200, completion_tokens=350, total_tokens=1550, cost=0.00042)
    response = completion(usage=usage)
    isolated_llm.create.return_value = response
    message = llm.chat(MESSAGES, agent="BudgetAgent", conversation_id="conv-1", temperature=0.1,
                       timeout=12, max_tokens=512)
    assert message.content == response.choices[0].message.content
    assert message.content == "Bonjour" and message.tool_calls is None
    kwargs = isolated_llm.create.call_args.kwargs
    assert kwargs == {"messages": MESSAGES, "model": "configured/model", "temperature": 0.1,
                      "timeout": 12, "max_tokens": 512}
    assert [event.action for event in isolated_llm.events] == ["LLM", "TOKEN", "COST", "INFO"]
    assert all(event.agent == "BudgetAgent" and event.conversation_id == "conv-1" for event in isolated_llm.events)
    entry = llm.USAGE_JOURNAL[0]
    assert entry["prompt_tokens"] == 1200 and entry["completion_tokens"] == 350
    assert entry["total_tokens"] == 1550 and entry["cost"] == 0.00042
    assert entry["model"] == "provider/model"
    assert entry["provider_request_id"] == "http-request-123"
    assert entry["provider_response_id"] == "gen-123"
    assert entry["duration_seconds"] >= 0
    assert datetime.fromisoformat(entry["timestamp"]).utcoffset() == timedelta(0)
    assert "Organise" not in json.dumps(entry) and "Bonjour" not in json.dumps(entry)


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", "tool_calls"])
def test_finish_reason_reaches_agent_and_usage_trace(reason, isolated_llm):
    response = completion(content="Partial or complete answer", usage=NS(prompt_tokens=20, completion_tokens=2048, total_tokens=2068))
    response.choices[0].finish_reason = reason
    isolated_llm.create.return_value = response
    answer = llm.chat(MESSAGES)
    assert answer.finish_reason == reason
    assert llm.USAGE_JOURNAL[-1]["finish_reason"] == reason
    assert llm.USAGE_JOURNAL[-1]["completion_tokens"] == 2048


def test_mapping_provider_response_is_supported(isolated_llm):
    isolated_llm.create.return_value = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Ready"}}],
                                       "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}
    assert llm.chat(MESSAGES) == {"role": "assistant", "content": "Ready", "finish_reason": "stop"}


def test_tool_call_not_executed_and_schema_sent(isolated_llm):
    tool = llm.tool_schema("search_venues", "Search venues", {"city": {"type": "string"}})
    isolated_llm.create.return_value = completion(content=None, tools=[function_call()])
    message = llm.chat(MESSAGES, tools=[tool])
    assert message.content is None
    assert llm.parse_arguments(message.tool_calls[0]) == {"city": "Namur"}
    assert isolated_llm.create.call_args.kwargs["tools"] == [tool]
    assert isolated_llm.create.call_args.kwargs["tool_choice"] == "auto"
    isolated_llm.create.assert_called_once()


def test_no_tools_and_optional_token_limit(isolated_llm):
    llm.chat(MESSAGES, tools=[], max_tokens=None)
    kwargs = isolated_llm.create.call_args.kwargs
    assert "tools" not in kwargs and "tool_choice" not in kwargs and "max_tokens" not in kwargs


@pytest.mark.parametrize("kwargs", [{"temperature": float("nan")}, {"temperature": 3}, {"timeout": 0},
                                     {"max_tokens": False}, {"retry_delays": [-1]},
                                     {"retry_delays": [100]}, {"max_retry_delay": float("inf")},
                                     {"tools": {}}, {"request_id": ""}, {"agent": ""}])
def test_invalid_options_make_no_request(kwargs, isolated_llm):
    with pytest.raises(llm.LLMError):
        llm.chat(MESSAGES, **kwargs)
    isolated_llm.create.assert_not_called()


def test_tool_schema_defaults_optional_properties_and_copies():
    properties = {"city": {"type": "string"}, "capacity": {"type": "integer"}}
    tool = llm.tool_schema("search", "Search", properties)
    assert tool["type"] == "function"
    assert tool["function"]["parameters"]["required"] == ["city", "capacity"]
    assert llm.tool_schema("search", "Search", properties, [])["function"]["parameters"]["required"] == []
    properties["city"]["type"] = "number"
    assert tool["function"]["parameters"]["properties"]["city"]["type"] == "string"
    with pytest.raises(llm.LLMError):
        llm.tool_schema("search", "Search", properties, ["missing"])


@pytest.mark.parametrize("arguments,expected", [(' {"city":"Liège","count":2} ', {"city": "Liège", "count": 2}),
                                                ("", {}), (None, {}), ("  ", {})])
def test_parse_arguments(arguments, expected):
    assert llm.parse_arguments(NS(function=NS(arguments=arguments))) == expected


@pytest.mark.parametrize("arguments", ["not JSON", "[]", "null", "42", '"text"', '{"x":NaN}',
                                        '{"nested": {"x": 1e999}}',
                                        "__import__('os').system('echo unsafe')", {}])
def test_invalid_arguments_rejected(arguments):
    with pytest.raises(llm.LLMError):
        llm.parse_arguments(NS(function=NS(arguments=arguments)))


def test_assistant_history_preserves_null_content_ids_and_arguments():
    tool = function_call('{ "city": "Liège" }')
    message = ChatCompletionMessage(role="assistant", content=None, tool_calls=[tool],
                                    reasoning="Do not persist private reasoning")
    history = llm.assistant_message(message)
    assert history == {"role": "assistant", "content": None, "tool_calls": [tool]}
    assert llm.assistant_message(ChatCompletionMessage(role="assistant", content="")) == {
        "role": "assistant", "content": "",
    }


def test_tool_result_unicode_and_non_json_errors():
    result = llm.tool_message(function_call(), {"venue": "Salle à Liège"})
    assert result == {"role": "tool", "tool_call_id": "call-1", "name": "search_venues",
                      "content": '{"venue": "Salle à Liège"}'}
    assert llm.tool_message(function_call(), "Ready")["content"] == "Ready"
    with pytest.raises(llm.LLMError):
        llm.tool_message(function_call(), object())


def test_request_context_and_unique_calls(isolated_llm):
    request_id = llm.new_request()
    llm.chat(MESSAGES, agent="A", conversation_id="c1")
    llm.chat(MESSAGES, agent="B", conversation_id="c1")
    first, second = llm.USAGE_JOURNAL
    assert first["request_id"] == second["request_id"] == request_id
    assert first["call_id"] != second["call_id"]
    assert first is not second
    assert llm.new_request() != request_id
    llm.chat(MESSAGES, request_id="explicit-request")
    assert llm.USAGE_JOURNAL[-1]["request_id"] == "explicit-request"
    assert Context().run(llm._current_request.get) is None
    assert llm.usage_summary(request_id=request_id)["total"]["calls"] == 2


def test_implicit_requests_are_unique(isolated_llm):
    llm.chat(MESSAGES)
    llm.chat(MESSAGES)
    assert llm.USAGE_JOURNAL[0]["request_id"] != llm.USAGE_JOURNAL[1]["request_id"]


def test_usage_aggregation_missing_values_and_conversation_filters(isolated_llm):
    isolated_llm.create.side_effect = [
        completion(usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 15, "cost": 0.2}),
        completion(usage={"prompt_tokens": 20, "completion_tokens": None, "cost": None}),
        completion(usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0}),
    ]
    llm.chat(MESSAGES, agent="A", conversation_id="c1")
    llm.chat(MESSAGES, agent="A", conversation_id="c2")
    llm.chat(MESSAGES, agent="B", conversation_id="c1")
    summary = llm.usage_summary()
    assert summary["agents"]["A"]["calls"] == 2
    assert summary["total"]["calls"] == 3
    assert summary["total"]["prompt_tokens"] == 30
    assert summary["total"]["total_tokens"] == 15  # Never recompute provider totals.
    assert summary["total"]["known_cost"] == 0.2
    assert summary["total"]["missing_cost_calls"] == 1
    assert not summary["total"]["cost_complete"]
    assert not summary["total"]["tokens_complete"]
    assert llm.usage_summary("c1")["total"]["cost_complete"]
    assert llm.usage_summary("c2")["total"]["known_cost"] is None
    assert llm.usage_summary("missing")["total"]["calls"] == 0
    assert llm.USAGE_JOURNAL[2]["cost"] == 0
    summary["agents"]["A"]["prompt_tokens"] = 999
    assert llm.usage_summary()["agents"]["A"]["prompt_tokens"] == 30


def test_unknown_usage_does_not_become_zero(isolated_llm):
    llm.chat(MESSAGES)
    entry = llm.USAGE_JOURNAL[0]
    assert all(entry[name] is None for name in (*llm._TOKEN_FIELDS, "cost"))
    assert llm.usage_summary()["total"]["known_cost"] is None
    assert "unavailable" in next(event.summary for event in isolated_llm.events if event.action == "COST")


def test_usage_summary_rich_table_unknown_partial_literal_and_disabled(monkeypatch, isolated_llm):
    isolated_llm.create.side_effect = [completion(usage={"prompt_tokens": 10, "cost": 0.3}), completion()]
    llm.chat(MESSAGES, agent="[red]Agent[/red]")
    llm.chat(MESSAGES, agent="[red]Agent[/red]")
    output = StringIO()
    monkeypatch.setattr(tracing, "console", Console(file=output, width=160, color_system=None))
    llm.display_usage_summary()
    assert output.getvalue() == ""
    tracing.enable_console()
    llm.display_usage_summary()
    rendered = output.getvalue()
    assert "[red]Agent[/red]" in rendered and "TOTAL" in rendered
    assert "partial" in rendered and "unknown" in rendered
    assert "$0.000000" not in rendered


@pytest.mark.parametrize("headers,expected", [({}, 2), ({"Retry-After": "3"}, 3),
                                               ({"retry-after-ms": "250"}, 0.25),
                                               ({"Retry-After": "bad"}, 2),
                                               ({"Retry-After": "nan"}, 2),
                                               ({"Retry-After": "-1"}, 2)])
def test_rate_limit_retry_headers_and_single_accounting(headers, expected, isolated_llm):
    isolated_llm.create.side_effect = [api_error(openai.RateLimitError, 429, headers), completion()]
    llm.chat(MESSAGES, retry_delays=[2])
    assert isolated_llm.create.call_count == 2
    llm.time.sleep.assert_called_once_with(expected)
    assert len(llm.USAGE_JOURNAL) == 1
    assert llm.USAGE_JOURNAL[0]["attempts"] == 2
    assert [event.action for event in isolated_llm.events].count("TOKEN") == 1
    assert any(event.metadata.get("retry_number") == 1 for event in isolated_llm.events)


def test_retry_after_http_date(isolated_llm):
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True)
    isolated_llm.create.side_effect = [api_error(openai.RateLimitError, 429, {"retry-after": future}), completion()]
    llm.chat(MESSAGES, retry_delays=[2])
    assert 8 < llm.time.sleep.call_args.args[0] <= 10


def test_retry_delay_above_limit_is_not_shortened(isolated_llm):
    isolated_llm.create.side_effect = api_error(openai.RateLimitError, 429, {"retry-after": "3600"})
    with pytest.raises(llm.LLMError, match="wait limit"):
        llm.chat(MESSAGES)
    isolated_llm.create.assert_called_once()
    llm.time.sleep.assert_not_called()


def test_rate_limit_exhaustion(isolated_llm):
    isolated_llm.create.side_effect = api_error(openai.RateLimitError, 429)
    with pytest.raises(llm.LLMError, match="429"):
        llm.chat(MESSAGES, retry_delays=[0, 0])
    assert isolated_llm.create.call_count == 3
    assert llm.USAGE_JOURNAL == []
    assert [event.action for event in isolated_llm.events].count("ERROR") == 1


@pytest.mark.parametrize("error_type,status", [(openai.AuthenticationError, 401),
                                                (openai.PermissionDeniedError, 403),
                                                (openai.BadRequestError, 400), (openai.NotFoundError, 404),
                                                (openai.InternalServerError, 500)])
def test_api_failures_safe_and_not_retried(error_type, status, isolated_llm):
    isolated_llm.create.side_effect = api_error(error_type, status)
    with pytest.raises(llm.LLMError) as exc:
        llm.chat(MESSAGES)
    isolated_llm.create.assert_called_once()
    assert SECRET not in str(exc.value)
    assert SECRET not in json.dumps([event.to_dict() for event in isolated_llm.events])
    assert exc.value.__suppress_context__
    assert llm.USAGE_JOURNAL == []


@pytest.mark.parametrize("kind,expected", [("timeout", "timed out"), ("connection", "connect"),
                                           ("unexpected", "unexpected")])
def test_transport_errors(kind, expected, isolated_llm):
    request = httpx2.Request("POST", "https://provider.invalid")
    failure = {"timeout": openai.APITimeoutError(request=request),
               "connection": openai.APIConnectionError(request=request, message=SECRET),
               "unexpected": RuntimeError(SECRET)}[kind]
    isolated_llm.create.side_effect = failure
    with pytest.raises(llm.LLMError, match=expected):
        llm.chat(MESSAGES)
    assert isolated_llm.create.call_count == 1


def test_sensitive_provider_identifiers_are_redacted(isolated_llm):
    isolated_llm.create.return_value = completion(model=SECRET, response_id=SECRET)
    llm.chat(MESSAGES)
    serialized = json.dumps(llm.USAGE_JOURNAL) + json.dumps([event.to_dict() for event in isolated_llm.events])
    assert SECRET not in serialized


@pytest.mark.parametrize("openrouter_shape", [False, True])
def test_streaming_text_final_usage_and_exactly_once(isolated_llm, openrouter_shape):
    usage = NS(prompt_tokens=10, completion_tokens=4, total_tokens=14, cost=0.002)
    stream = FakeStream([chunk("Bon"), chunk("jour"), chunk(finish="stop"),
                         chunk(choices=openrouter_shape, finish="stop" if openrouter_shape else None, usage=usage)])
    isolated_llm.create.return_value = stream
    iterator = llm.stream_chat(MESSAGES, agent="Writer", conversation_id="c1")
    assert next(iterator)["text"] == "Bon"
    assert llm.USAGE_JOURNAL == []
    remaining = list(iterator)
    assert [event["type"] for event in remaining] == ["text_delta", "completed"]
    assert remaining[-1]["message"].content == "Bonjour"
    assert remaining[-1]["usage"]["cost"] == 0.002
    assert remaining[-1]["usage"]["provider_response_id"] == "gen-stream"
    assert stream.closed and len(llm.USAGE_JOURNAL) == 1
    assert [event.action for event in isolated_llm.events].count("TOKEN") == 1
    assert [event.action for event in isolated_llm.events].count("COST") == 1
    assert isolated_llm.create.call_args.kwargs["stream_options"] == {"include_usage": True}
    remaining[-1]["usage"]["cost"] = 999
    assert llm.USAGE_JOURNAL[0]["cost"] == 0.002
    assert "Bonjour" not in json.dumps([event.to_dict() for event in isolated_llm.events])


def test_streamed_tools_reconstructed_by_index(isolated_llm):
    stream = FakeStream([
        chunk(tools=[delta(1, "call-b", "get_budget", '{"amount":'), delta(0, "call-a", "search_", '{"city":')]),
        chunk(tools=[delta(0, name="venues", arguments='"Liège"}'), delta(1, arguments="5000}")]),
        chunk(finish="tool_calls"),
    ])
    isolated_llm.create.return_value = stream
    events = list(llm.stream_chat(MESSAGES, tools=[llm.tool_schema("search_venues", "Search", {})]))
    assert [event["type"] for event in events].count("tool_call_delta") == 4
    message = events[-1]["message"]
    assert message.content is None
    assert [tool.id for tool in message.tool_calls] == ["call-a", "call-b"]
    assert message.tool_calls[0].function.name == "search_venues"
    assert llm.parse_arguments(message.tool_calls[0]) == {"city": "Liège"}
    assert llm.parse_arguments(message.tool_calls[1]) == {"amount": 5000}
    history = llm.assistant_message(message)
    assert history["tool_calls"][1]["function"]["arguments"] == '{"amount":5000}'
    assert llm.USAGE_JOURNAL[0]["total_tokens"] is None


def test_stream_without_usage_and_stream_options_opt_out(isolated_llm):
    isolated_llm.create.return_value = FakeStream([chunk("Ready"), chunk(finish="stop")])
    events = list(llm.stream_chat(MESSAGES, include_usage=False))
    assert "stream_options" not in isolated_llm.create.call_args.kwargs
    assert events[-1]["usage"]["cost"] is None
    assert events[-1]["usage"]["total_tokens"] is None


def test_stream_rate_limit_before_opening(isolated_llm):
    stream = FakeStream([chunk("Ready"), chunk(finish="stop")])
    isolated_llm.create.side_effect = [api_error(openai.RateLimitError, 429), stream]
    list(llm.stream_chat(MESSAGES, retry_delays=[0]))
    assert len(llm.USAGE_JOURNAL) == 1 and stream.closed
    assert isolated_llm.create.call_count == 2


def test_stream_interruption_after_output_is_not_retried(isolated_llm):
    stream = FakeStream([chunk("Partial"), api_error(openai.RateLimitError, 429)])
    isolated_llm.create.return_value = stream
    iterator = llm.stream_chat(MESSAGES)
    assert next(iterator)["text"] == "Partial"
    with pytest.raises(llm.LLMError):
        next(iterator)
    assert stream.closed and llm.USAGE_JOURNAL == []
    isolated_llm.create.assert_called_once()
    llm.time.sleep.assert_not_called()
    assert not any(event.action in {"TOKEN", "COST"} for event in isolated_llm.events)


def test_stream_close_cancels_and_cleans_up(isolated_llm):
    stream = FakeStream([chunk("Partial"), chunk("More"), chunk(finish="stop")])
    isolated_llm.create.return_value = stream
    with closing(llm.stream_chat(MESSAGES)) as iterator:
        assert next(iterator)["text"] == "Partial"
    assert stream.closed and llm.USAGE_JOURNAL == []
    assert "cancelled" in isolated_llm.events[-1].summary


def test_stream_keyboard_interrupt(isolated_llm):
    stream = FakeStream([chunk("Partial"), KeyboardInterrupt()])
    isolated_llm.create.return_value = stream
    with pytest.raises(KeyboardInterrupt):
        list(llm.stream_chat(MESSAGES))
    assert stream.closed and llm.USAGE_JOURNAL == []


@pytest.mark.parametrize("items", [[], [chunk("Truncated")],
                                    [chunk(error={"message": SECRET}, finish="error")],
                                    [chunk(tools=[delta(arguments="{}")]), chunk(finish="tool_calls")]])
def test_malformed_or_error_streams(items, isolated_llm):
    stream = FakeStream(items)
    isolated_llm.create.return_value = stream
    with pytest.raises(llm.LLMError) as exc:
        list(llm.stream_chat(MESSAGES))
    assert SECRET not in str(exc.value)
    assert stream.closed and llm.USAGE_JOURNAL == []


def test_sdk_non_streaming_with_mock_http_transport(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "configured/model"
        assert body["tools"][0]["function"]["name"] == "search"
        return httpx2.Response(200, headers={"x-request-id": "http-real-sdk"}, json={
            "id": "gen-real-sdk", "object": "chat.completion", "created": 1, "model": "provider/model",
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None, "tool_calls": [function_call()]}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6, "cost": 0.001},
        })

    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as http_client:
        monkeypatch.setattr(llm, "OpenAI", lambda **kwargs: openai.OpenAI(**kwargs, http_client=http_client))
        message = llm.chat(MESSAGES, tools=[llm.tool_schema("search", "Search", {})])
        assert message.tool_calls[0].id == "call-1"
        assert llm.USAGE_JOURNAL[0]["cost"] == 0.001
        assert llm.USAGE_JOURNAL[0]["provider_request_id"] == "http-real-sdk"


def test_sdk_streaming_with_mock_sse_transport(monkeypatch):
    frames = [
        {"choices": [{"index": 0, "delta": {"content": "Bonjour"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6, "cost": 0.001}},
    ]
    body = "".join("data: " + json.dumps({"id": "gen-sse", "model": "provider/model", "created": 1,
                                          "object": "chat.completion.chunk"} | frame) + "\n\n" for frame in frames)
    body += "data: [DONE]\n\n"
    transport = httpx2.MockTransport(lambda request: httpx2.Response(
        200, text=body, headers={"Content-Type": "text/event-stream", "x-request-id": "http-sse"},
    ))
    with httpx2.Client(transport=transport) as http_client:
        monkeypatch.setattr(llm, "OpenAI", lambda **kwargs: openai.OpenAI(**kwargs, http_client=http_client))
        events = list(llm.stream_chat(MESSAGES))
        assert events[-1]["message"].content == "Bonjour"
        assert events[-1]["usage"]["total_tokens"] == 6
        assert events[-1]["usage"]["cost"] == 0.001
        assert events[-1]["usage"]["provider_request_id"] == "http-sse"


def test_chat_malformed_completed_response_still_accounts_for_usage(isolated_llm):
    response = completion(usage={"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10})
    response.choices = []
    isolated_llm.create.return_value = response
    with pytest.raises(llm.LLMError, match="no assistant message"):
        llm.chat(MESSAGES)
    assert len(llm.USAGE_JOURNAL) == 1
    assert llm.USAGE_JOURNAL[0]["total_tokens"] == 10
    isolated_llm.create.assert_called_once()


def test_stream_refusal_preserved(isolated_llm):
    isolated_llm.create.return_value = FakeStream([
        chunk(refusal="Cannot "), chunk(refusal="help."), chunk(finish="stop"),
    ])
    events = list(llm.stream_chat(MESSAGES))
    assert events[0]["type"] == "refusal_delta"
    assert llm.assistant_message(events[-1]["message"])["refusal"] == "Cannot help."


def test_stream_cleanup_failure_does_not_mask_completion(isolated_llm):
    stream = FakeStream([chunk("Ready"), chunk(finish="stop")])
    stream.close = Mock(side_effect=OSError(SECRET))
    isolated_llm.create.return_value = stream
    events = list(llm.stream_chat(MESSAGES))
    assert events[-1]["type"] == "completed"
    assert len(llm.USAGE_JOURNAL) == 1
    assert SECRET not in json.dumps([event.to_dict() for event in isolated_llm.events])
