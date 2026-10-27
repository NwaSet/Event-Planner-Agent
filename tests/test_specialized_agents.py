"""Offline architecture and integration checks; no claims about LLM decisions."""

import builtins
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import agents
from agents import (
    create_budget_agent,
    create_provider_agent,
    create_schedule_agent,
    create_venue_agent,
    create_writer_agent,
)
from dao import ConversationDAO, MemoryDAO
from model import agent as agent_module
from model import llm, trace as tracing
from model.agent import Agent
from tools import budget_tools, data_tools, schedule_tools, tool_registry, search_tools


_ITEM = {"get_item": data_tools.get_item}
_BUDGET = {name: getattr(budget_tools, name) for name in (
    "calculate_total", "remaining_budget", "validate_budget",
)}
_VENUE = {"search_venues": search_tools.search_venues, "get_venue": data_tools.get_venue}
_PROVIDER = {"search_providers": search_tools.search_providers, "get_provider": data_tools.get_provider,
             "search_caterers": search_tools.search_caterers, "get_caterer": data_tools.get_caterer}
_SCHEDULE = {name: getattr(schedule_tools, name) for name in (
    "validate_schedule", "detect_conflicts", "sort_activities",
)}
_CASES = [
    (create_budget_agent, "BudgetAgent", _BUDGET | _ITEM),
    (create_venue_agent, "VenueAgent", _VENUE | _BUDGET),
    (create_provider_agent, "ProviderAgent", _PROVIDER | _BUDGET | _ITEM),
    (create_schedule_agent, "ScheduleAgent", _SCHEDULE | _ITEM),
    (create_writer_agent, "WriterAgent", _ITEM),
]
_FACTORIES = [case[0] for case in _CASES]


def _message(content=None, calls=None):
    return {"role": "assistant", "content": content, "tool_calls": calls or []}


def _call(name, arguments=None, identifier="call_1"):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {}),
    }}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    chat = Mock(return_value=_message("Draft ready."))
    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "get_client", Mock(side_effect=AssertionError("No paid requests.")))
    monkeypatch.setattr(llm, "load_dotenv", Mock(side_effect=AssertionError("No .env reads.")))
    monkeypatch.setattr(builtins, "input", Mock(side_effect=AssertionError("No terminal input.")))
    monkeypatch.setattr(tracing, "_listeners", [])
    monkeypatch.setattr(tracing, "_console_enabled", False)
    monkeypatch.setattr(tracing, "_colors_agents", {})
    token = agent_module._context.set(None)
    yield chat
    agent_module._context.reset(token)


@pytest.mark.parametrize("factory,name,functions", _CASES)
def test_factory_contract_and_exact_tool_permissions(factory, name, functions, offline):
    agent = factory()
    assert type(agent) is Agent
    assert agent.name == name
    assert 1 <= agent.max_iterations <= 24
    assert agent.functions == functions
    assert agent.get_history() == [{"role": "system", "content": agent.instructions}]
    assert name in agent.instructions
    assert len(agent.schemas) == len(functions)
    registry = {
        schema["function"]["name"]: schema
        for group in (tool_registry.get_budget_tools, tool_registry.get_venue_tools,
                      tool_registry.get_provider_tools, tool_registry.get_caterer_tools,
                      tool_registry.get_schedule_tools, tool_registry.get_item_tools)
        for _, schema in group()
    }
    assert {schema["function"]["name"]: schema for schema in agent.schemas} == {
        name: registry[name] for name in functions
    }
    offline.assert_not_called()


def test_public_factories_have_unique_agent_names():
    assert set(agents.__all__) == {factory.__name__ for factory in _FACTORIES}
    assert len({factory().name for factory in _FACTORIES}) == 5


@pytest.mark.parametrize("factory,required", [
    (create_venue_agent, ["search_venues", '"selected"', '"items"', "indoor_only=true", "not_found"]),
    (create_provider_agent, ["meal=false", "drinks/bar are separate", '"option_id"', "no catalogue fields"]),
    (create_budget_agent, ["get_item", "calculate_total", "remaining_budget", "unknown costs are not zero"]),
    (create_schedule_agent, ["get_item", "validate_schedule", "detect_conflicts", "missing_information"]),
    (create_writer_agent, ["get_item", "no search tools", "never restart selection", "readable text"]),
])
def test_role_contract(factory, required):
    prompt = " ".join(factory().instructions.lower().split())
    for rule in required:
        assert rule in prompt


@pytest.mark.parametrize("factory", _FACTORIES)
def test_mocked_response_and_llm_context(factory, offline):
    agent = factory()
    offline.return_value = _message("Voici les informations manquantes.")
    assert agent.run("Organise mon événement.", conversation_id="conv_001") == (
        "Voici les informations manquantes."
    )
    offline.assert_called_once()
    assert offline.call_args.args[0] == [
        {"role": "system", "content": agent.instructions},
        {"role": "user", "content": "Organise mon événement."},
    ]
    assert offline.call_args.kwargs["tools"] == (agent.schemas or None)
    assert offline.call_args.kwargs["agent"] == agent.name
    assert offline.call_args.kwargs["conversation_id"] == "conv_001"
    assert offline.call_args.kwargs["request_id"]


@pytest.mark.parametrize("factory,clarification", [
    (create_budget_agent, "Please provide the total budget and tax basis."),
    (create_venue_agent, "Please provide attendance, location, and venue allocation."),
    (create_provider_agent, "Please provide required services and venue restrictions."),
    (create_schedule_agent, "Please provide the event date and timezone."),
    (create_writer_agent, "Draft only. Recipient address: unspecified. Event date: unspecified."),
])
def test_missing_context_is_passed_to_llm_without_filling_defaults(factory, clarification, offline):
    # This verifies integration and instructions, not actual model compliance.
    offline.return_value = _message(clarification)
    agent = factory()
    assert agent.run("Help plan an event.", "conv_missing") == clarification
    assert offline.call_args.args[0][-1] == {"role": "user", "content": "Help plan an event."}
    assert len(agent.get_history("conv_missing")) == 3


@pytest.mark.parametrize("factory", _FACTORIES)
def test_memory_isolated_between_agents_instances_and_conversations(factory, offline):
    agent, other_instance = factory(), factory()
    others = [other() for other in _FACTORIES if other is not factory]
    agent.run("First private request", "conv_001")
    agent.run("Second conversation", "conv_002")
    agent.run("Follow up", "conv_001")
    sent = offline.call_args.args[0]
    assert [message["content"] for message in sent if message["role"] == "user"] == [
        "Follow up",
    ]
    for untouched in [other_instance, *others]:
        assert untouched.get_history("conv_001") == [
            {"role": "system", "content": untouched.instructions},
        ]
    assert len(agent.get_history()) == 1
    exported = agent.get_history("conv_001")
    exported.clear()
    assert len(agent.get_history("conv_001")) == 5
    agent.clear_history("conv_001")
    assert len(agent.get_history("conv_002")) == 3


@pytest.mark.parametrize("factory", _FACTORIES)
def test_delegation_inherits_conversation_and_request(factory, offline):
    specialist = factory()
    definition = specialist.as_tool("ask_specialist", "Ask the event specialist.")
    assert definition[1]["function"]["parameters"]["required"] == ["request"]
    supervisor = Agent("TestSupervisor", "Delegate this request.", tools=[definition])
    offline.side_effect = [
        _message(calls=[_call("ask_specialist", {"request": "Analyze supplied context."})]),
        _message("Specialist response."),
        _message("Supervisor response."),
    ]
    assert supervisor.run("Help plan.", "conv_delegate") == "Supervisor response."
    calls = offline.call_args_list
    assert [call.kwargs["agent"] for call in calls] == ["TestSupervisor", specialist.name, "TestSupervisor"]
    assert {call.kwargs["conversation_id"] for call in calls} == {"conv_delegate"}
    assert len({call.kwargs["request_id"] for call in calls}) == 1
    assert specialist.get_history("conv_delegate")[-1]["content"] == "Specialist response."
    assert len(specialist.get_history()) == 1


@pytest.mark.parametrize("factory", _FACTORIES)
def test_existing_dao_can_round_trip_memory_explicitly(factory, offline, tmp_path):
    conversation_id = ConversationDAO(tmp_path).create_conversation("Specialist test")
    original = factory()
    original.run("Remember the event requirements.", conversation_id)
    dao = MemoryDAO(tmp_path)
    dao.save_memory(conversation_id, original.name, original.get_history(conversation_id))
    restored = factory()
    restored.load_history(dao.load_memory(conversation_id, original.name), conversation_id)
    assert restored.get_history(conversation_id) == original.get_history(conversation_id)
    assert dao.load_memory(conversation_id, "UnrelatedAgent") is None
    restored.run("Continue planning.", conversation_id)
    assert len(original.get_history(conversation_id)) == 3
    assert len(restored.get_history(conversation_id)) == 5


@pytest.mark.parametrize("factory", _FACTORIES)
@pytest.mark.parametrize("tool_name", ["send_email", "make_reservation", "write_json"])
def test_unauthorized_actions_are_rejected_even_if_requested_by_llm(factory, tool_name, offline):
    offline.side_effect = [
        _message(calls=[_call(tool_name)]),
        _message("No external action performed."),
    ]
    agent = factory()
    assert agent.run("Send an email and reserve the venue.") == "No external action performed."
    observation = json.loads(offline.call_args.args[0][-1]["content"])
    assert "not registered" in observation["error"]


@pytest.mark.parametrize("factory,tool_name,arguments,result_key", [
    (create_budget_agent, "validate_budget", {"total_budget": 100, "allocations": {"venue": 120}}, "valid"),
    (create_venue_agent, "get_venue", {"venue_id": "salle_001"}, "id"),
    (create_provider_agent, "get_provider", {"provider_id": "001"}, "id"),
    (create_provider_agent, "get_caterer", {"caterer_id": "traiteur_001"}, "id"),
    (create_schedule_agent, "validate_schedule", {"activities": [
        {"name": "Invalid duration", "start": "2026-10-26T18:00", "end": "2026-10-26T17:00"},
    ]}, "valid"),
])
def test_real_tool_results_reach_mocked_llm(factory, tool_name, arguments, result_key, offline):
    offline.side_effect = [_message(calls=[_call(tool_name, arguments)]), _message("Review complete.")]
    agent = factory()
    assert agent.run("Review the supplied information.") == "Review complete."
    observation = json.loads(offline.call_args.args[0][-1]["content"])
    assert result_key in observation
    if result_key == "valid":
        assert observation["valid"] is False
    else:
        assert observation["id"] == next(iter(arguments.values()))


def test_schedule_conflict_is_exposed_for_revision(offline):
    activities = [
        {"name": "Setup", "start": "2026-10-26T17:00", "end": "2026-10-26T19:00", "resources": ["stage"]},
        {"name": "Show", "start": "2026-10-26T18:00", "end": "2026-10-26T20:00", "resources": ["stage"]},
    ]
    offline.side_effect = [
        _message(calls=[_call("validate_schedule", {"activities": activities})]),
        _message(calls=[_call("detect_conflicts", {"activities": activities}, "call_2")]),
        _message("Stage overlap remains unresolved."),
    ]
    assert create_schedule_agent().run("Check this timeline.") == "Stage overlap remains unresolved."
    conflicts = json.loads(offline.call_args.args[0][-1]["content"])
    assert conflicts[0]["resources"] == ["stage"]
    assert conflicts[0]["activity_names"] == ["Setup", "Show"]


def test_missing_dataset_failure_is_not_replaced_with_fabricated_records(monkeypatch, offline, tmp_path):
    monkeypatch.setattr(data_tools, "_DATA_DIR", tmp_path)
    offline.side_effect = [
        _message(calls=[_call("search_venues")]),
        _message("Venue data unavailable; no recommendation can be verified."),
    ]
    assert "unavailable" in create_venue_agent().run("Find a venue.")
    result = json.loads(offline.call_args.args[0][-1]["content"])
    assert "no usable result" in result["error"]
    assert list(tmp_path.iterdir()) == []


def test_factories_do_not_share_mutable_tool_configuration():
    first, second = create_provider_agent(), create_provider_agent()
    first.schemas[0]["function"]["description"] = "Changed"
    first.functions.clear()
    assert second.functions == _PROVIDER | _BUDGET | _ITEM
    assert second.schemas[0]["function"]["description"] != "Changed"
    assert tool_registry.get_provider_tools()[0][1]["function"]["description"] != "Changed"


def test_creation_and_runs_never_write_json_or_read_data_at_construction(monkeypatch, offline):
    root = Path(__file__).resolve().parents[1]
    paths = [*sorted((root / "data").glob("*.json")), *sorted((root / "logs").glob("*.json"))]
    before = {path: path.read_bytes() for path in paths}
    real_open, real_io_open = builtins.open, io.open

    def forbid_io(*args, **kwargs):
        raise AssertionError("Factory construction must not access files.")

    with monkeypatch.context() as guarded:
        guarded.setattr(builtins, "open", forbid_io)
        guarded.setattr(io, "open", forbid_io)
        specialists = [factory() for factory in _FACTORIES]

    def read_only(opener):
        def guarded(file, mode="r", *args, **kwargs):
            assert not any(flag in mode for flag in "wax+"), "Specialists must not write files."
            return opener(file, mode, *args, **kwargs)
        return guarded

    with monkeypatch.context() as guarded:
        guarded.setattr(builtins, "open", read_only(real_open))
        guarded.setattr(io, "open", read_only(real_io_open))
        for agent in specialists:
            if "list_datasets" in agent.functions:
                offline.side_effect = [
                    _message(calls=[_call("list_datasets")]), _message("Data inspected."),
                ]
            else:
                offline.side_effect = None
            agent.run("Prepare an event draft.", "conv_readonly")
    assert {path: path.read_bytes() for path in paths} == before
