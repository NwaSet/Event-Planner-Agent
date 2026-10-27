"""Registry and real Agent integration without any LLM or network access."""

from copy import deepcopy
import inspect
import json
from unittest.mock import Mock

from openai.types.chat import ChatCompletionMessage
import pytest

from model.agent import Agent
from model import llm, trace
from tools import tool_registry as registry


GROUPS = [registry.get_venue_tools, registry.get_provider_tools, registry.get_caterer_tools,
          registry.get_budget_tools, registry.get_schedule_tools]


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    monkeypatch.setattr(llm, "chat", Mock(side_effect=AssertionError("No real LLM calls are allowed.")))
    monkeypatch.setattr(llm, "OpenAI", Mock(side_effect=AssertionError("No network clients are allowed.")))
    monkeypatch.setattr(llm, "load_dotenv", Mock(side_effect=AssertionError("Tests must not read .env.")))
    monkeypatch.setattr(trace, "_listeners", [])
    monkeypatch.setattr(trace, "_console_enabled", False)
    monkeypatch.setattr(trace, "_colors_agents", {})


@pytest.mark.parametrize("group", GROUPS)
def test_groups_are_compatible_with_agent_and_function_signatures(group):
    pairs = group()
    agent = Agent("TestAgent", "Use deterministic tools.", tools=pairs)
    assert len(agent.functions) == len(pairs)
    for function, schema in pairs:
        spec = schema["function"]
        assert spec["name"] == function.__name__
        assert set(spec["parameters"]["properties"]) == set(inspect.signature(function).parameters)
        required = {name for name, parameter in inspect.signature(function).parameters.items()
                    if parameter.default is inspect.Parameter.empty}
        assert set(spec["parameters"]["required"]) == required
        assert spec["parameters"]["additionalProperties"] is False
        json.dumps(schema, allow_nan=False)


def test_roles_receive_only_relevant_tools():
    names = lambda group: {schema["function"]["name"] for _, schema in group()}
    assert names(registry.get_venue_tools) == {"search_venues", "get_venue"}
    assert names(registry.get_provider_tools) == {"search_providers", "get_provider"}
    assert names(registry.get_caterer_tools) == {"search_caterers", "get_caterer"}
    assert names(registry.get_budget_tools) == {"calculate_total", "remaining_budget", "validate_budget"}
    assert names(registry.get_schedule_tools) == {"validate_schedule", "detect_conflicts", "sort_activities"}


@pytest.mark.parametrize("group", GROUPS)
def test_registry_returns_independent_schema_copies(group):
    first, second = group(), group()
    expected = deepcopy(second)
    first[0][1]["function"]["description"] = "Changed locally"
    first[0][1]["function"]["parameters"]["properties"]["extra"] = {"type": "string"}
    assert group() == expected and second == expected


@pytest.mark.parametrize("group,name,arguments,expected", [
    (registry.get_venue_tools, "get_venue", {"venue_id": "salle_001"}, "salle_001"),
    (registry.get_provider_tools, "get_provider", {"provider_id": "001"}, "001"),
    (registry.get_caterer_tools, "get_caterer", {"caterer_id": "traiteur_001"}, "traiteur_001"),
])
def test_agent_executes_actual_data_tools_with_single_trace_pair(group, name, arguments, expected):
    events = []
    trace.define_listener(events.append)
    agent = Agent("TestAgent", "Read data.", tools=group())
    output = agent.execute_tool({"id": "call-1", "type": "function",
                                 "function": {"name": name, "arguments": json.dumps(arguments)}})
    assert json.loads(output)["id"] == expected
    assert [event.action for event in events] == ["ACTION", "OBSERV"]


def test_mocked_react_loop_with_real_budget_tool(monkeypatch):
    calls = [{"id": "call-budget", "type": "function", "function": {
        "name": "calculate_total", "arguments": '{"items":[{"name":"A","price":0.1},{"name":"B","price":0.2}]}',
    }}]
    chat = Mock(side_effect=[ChatCompletionMessage(role="assistant", content=None, tool_calls=calls),
                             ChatCompletionMessage(role="assistant", content="The total is EUR 0.30.")])
    monkeypatch.setattr(llm, "chat", chat)
    agent = Agent("TestAgent", "Check totals.", tools=registry.get_budget_tools())
    assert agent.run("Add these amounts", "test-conversation") == "The total is EUR 0.30."
    assert json.loads(agent.history[3]["content"])["total"] == "0.30"
    assert chat.call_count == 2


def test_direct_tools_emit_no_trace_events():
    events = []
    trace.define_listener(events.append)
    from tools.data_tools import get_venues
    from tools.budget_tools import calculate_total
    from tools.schedule_tools import validate_schedule

    get_venues(limit=1)
    calculate_total([])
    validate_schedule([])
    assert events == []
