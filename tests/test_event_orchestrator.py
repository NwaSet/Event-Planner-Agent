"""Controlled LLM/tool conversations against real agents and temporary DAOs."""

from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dao import ConversationDAO, MemoryDAO
from dao import json_storage
from dao.json_storage import StorageIOError
from model import llm, trace
from model.agent import Agent
from services import EventOrchestrator, OrchestratorResult
from tools import budget_tools, data_tools


def message(text=None, calls=None):
    return {"role": "assistant", "content": text, "tool_calls": calls or []}


def call(name, arguments=None, identifier="call_1"):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False),
    }}


def action(name, arguments=None):
    return ("Orchestrator", message(calls=[call(name, arguments)]))


def finish(text="Proposal ready.", status="completed", questions=None):
    return [action("submit_result", {"status": status, "response": text, "required_information": questions or []}),
            ("Orchestrator", message(text))]


def context(snapshot):
    return action("update_planning_context", {"context": snapshot})


def delegate(role, request="Review requirements.", answer="Specialist recommendation."):
    tool_name = f"ask_{role}_agent"
    return [action(tool_name, {"request": request}), (f"{role.title()}Agent", message(answer))]


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(json_storage, "_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(trace, "_listeners", [])
    monkeypatch.setattr(trace, "_console_enabled", False)
    monkeypatch.setattr(trace, "_colors_agents", {})
    monkeypatch.setattr(llm, "USAGE_JOURNAL", [])
    monkeypatch.setattr(llm, "get_client", Mock(side_effect=AssertionError("Paid LLM calls are forbidden.")))
    monkeypatch.setattr(llm, "load_dotenv", Mock(side_effect=AssertionError("Credentials must not be read.")))
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("No terminal input.")))
    chat = Mock(side_effect=AssertionError("A scripted response is required."))
    monkeypatch.setattr(llm, "chat", chat)
    events = []
    trace.define_listener(events.append)
    return SimpleNamespace(chat=chat, events=events, directory=tmp_path / "logs")


def script(offline, steps):
    queue = iter(steps)

    def respond(messages, **kwargs):
        role, response = next(queue)
        assert kwargs["agent"] == role
        if isinstance(response, BaseException):
            raise response
        return response

    offline.chat.side_effect = respond


def test_initialization_is_lazy_and_registers_only_authorized_tools(offline):
    service = EventOrchestrator()
    assert not offline.directory.exists()
    assert len(trace._listeners) == 1
    assert type(service.supervisor) is Agent
    assert {agent.name for agent in service.agents} == {
        "Orchestrator", "BudgetAgent", "VenueAgent", "ProviderAgent", "ScheduleAgent", "WriterAgent",
    }
    assert set(service.supervisor.functions) == {
        "ask_budget_agent", "ask_venue_agent", "ask_provider_agent", "ask_schedule_agent", "ask_writer_agent",
        "update_planning_context", "submit_result",
    }
    offline.chat.assert_not_called()
    prompt = " ".join(service.supervisor.instructions.split())
    for rule in ("user's language", "no fixed agent order", "Never invent venues", "assume human approval",
                 "drafts only", "mandatory constraints", "Specialists do not share memories"):
        assert rule in prompt


@pytest.mark.parametrize("options", [{"max_iterations": 0}, {"max_delegations": True}, {"max_calls_per_agent": -1}])
def test_invalid_limits_are_rejected(options):
    with pytest.raises(ValueError):
        EventOrchestrator(**options)


def test_new_conversation_and_actual_memories_are_persisted(offline):
    script(offline, finish("Bonjour, voici la proposition."))
    service = EventOrchestrator()
    result = service.run("Organise une fête.")
    assert isinstance(result, OrchestratorResult)
    assert result.status == "completed" and result.persisted
    assert result.response == "Bonjour, voici la proposition."
    stored = service.conversation_dao.get_conversation(result.conversation_id)
    assert stored["status"] == "active"
    assert [item["role"] for item in stored["messages"]] == ["user", "assistant"]
    assert len({item["id"] for item in stored["messages"]}) == 2
    for agent in service.agents:
        assert stored["agent_memories"][agent.name] == agent.get_history(result.conversation_id)
    assert len(stored["agent_memories"]["Orchestrator"]) == 5
    json.dumps(asdict(result), allow_nan=False)


@pytest.mark.parametrize("role", ["budget", "venue", "provider", "schedule", "writer"])
def test_individual_delegation_uses_existing_as_tool_context(role, offline):
    script(offline, [*delegate(role), *finish()])
    service = EventOrchestrator()
    result = service.run("Review this part of the event.")
    assert result.status == "completed"
    calls = offline.chat.call_args_list
    assert [entry.kwargs["agent"] for entry in calls] == ["Orchestrator", f"{role.title()}Agent", "Orchestrator", "Orchestrator"]
    assert {entry.kwargs["conversation_id"] for entry in calls} == {result.conversation_id}
    assert len({entry.kwargs["request_id"] for entry in calls}) == 1
    task = json.loads(calls[1].args[0][-1]["content"])
    assert task["task"] == "Review requirements."
    assert "planning_context" in task
    assert result.partial_results == {f"{role.title()}Agent": "Specialist recommendation."}


def test_dynamic_multi_agent_order_and_explicit_context_transfer(offline):
    snapshot = {"requirements": {"attendance": 100, "city": "Namur"},
                "budget": {"total_budget": 5000, "currency": "EUR", "tax_basis": "TTC",
                           "allocations": {"venue": 1500, "providers": 2500, "other": 1000}}}
    script(offline, [*delegate("budget", answer="Venue ceiling 1500 EUR; provider ceiling 2500 EUR."),
                     context(snapshot), *delegate("venue", "Compare venues within the 1500 EUR allocation."),
                     *delegate("provider", "Respect the 2500 EUR allocation and the venue restrictions above."),
                     *delegate("writer", "Summarize the supplied findings and remaining unknowns."), *finish()])
    result = EventOrchestrator().run("Plan the event using the supplied requirements.")
    assert result.status == "completed"
    for entry in offline.chat.call_args_list:
        if entry.kwargs["agent"] in {"VenueAgent", "ProviderAgent", "WriterAgent"}:
            supplied = json.loads(entry.args[0][-1]["content"])
            assert supplied["planning_context"]["budget"]["allocations"]["venue"] == 1500
            assert supplied["planning_context"]["requirements"]["city"] == "Namur"


def test_restart_restores_supervisor_specialists_and_compact_plan(offline):
    snapshot = {"requirements": {"attendance": 150}, "preferences": {"parking": True},
                "decisions": ["Prefer a venue in Namur."]}
    script(offline, [context(snapshot), *delegate("venue"), *finish("First proposal.")])
    original = EventOrchestrator()
    first = original.run("Find a venue.")
    expected_supervisor = original.supervisor.get_history(first.conversation_id)
    expected_venue = original.venue_agent.get_history(first.conversation_id)
    restored = EventOrchestrator()
    assert restored.get_planning_context(first.conversation_id) == first.planning_context
    script(offline, [*delegate("venue", "Reconsider using the existing preferences."), *finish("Updated proposal.")])
    start = offline.chat.call_count
    second = restored.run("Can it have more parking?", first.conversation_id)
    calls = offline.chat.call_args_list[start:]
    assert calls[0].args[0][:-1] == expected_supervisor
    assert calls[1].args[0][:-1] == expected_venue
    assert json.loads(calls[1].args[0][-1]["content"])["planning_context"]["preferences"] == {"parking": True}
    assert second.conversation_id == first.conversation_id and second.status == "completed"
    visible = restored.conversation_dao.get_messages(first.conversation_id)
    assert [item["content"] for item in visible] == ["Find a venue.", "First proposal.", "Can it have more parking?", "Updated proposal."]


def test_conversations_and_absent_memories_do_not_reuse_stale_state(offline):
    service = EventOrchestrator()
    script(offline, [context({"preferences": {"private_marker": "first"}}), *delegate("venue"), *finish()])
    first = service.run("First private event.")
    script(offline, [*delegate("venue"), *finish()])
    start = offline.chat.call_count
    second = service.run("Second event.")
    for entry in offline.chat.call_args_list[start:]:
        assert "private_marker" not in json.dumps(entry.args[0])
    assert second.conversation_id != first.conversation_id
    service.memory_dao.delete_memory(first.conversation_id, "VenueAgent")
    script(offline, [*delegate("venue"), *finish()])
    start = offline.chat.call_count
    service.run("Restart venue analysis.", first.conversation_id)
    assert len(offline.chat.call_args_list[start + 1].args[0]) == 2


def test_unknown_id_does_not_create_a_conversation(offline):
    service = EventOrchestrator()
    with pytest.raises(LookupError):
        service.run("Continue.", "conv_unknown")
    assert service.conversation_dao.list_conversations() == []
    offline.chat.assert_not_called()


@pytest.mark.parametrize("user_request", ["", "  ", None, 12])
def test_invalid_request_does_not_persist_or_call_llm(user_request, offline):
    with pytest.raises(ValueError):
        EventOrchestrator().run(user_request)
    assert not offline.directory.exists()


def test_needs_input_is_structured_not_inferred_from_prose(offline):
    questions = ["Quelle date souhaitez-vous ?", "Dans quelle ville ?"]
    script(offline, finish("Merci de préciser la date et la ville.", "needs_input", questions))
    service = EventOrchestrator()
    result = service.run("Organise un anniversaire.")
    assert result.status == "needs_input" and result.required_information == questions
    assert result.persisted
    assert [event.action for event in offline.events].count("HUMAN") == 1
    script(offline, finish("Les informations sont enregistrées."))
    continued = service.run("À Namur le 30 novembre 2026.", result.conversation_id)
    assert continued.status == "completed" and continued.required_information == []


@pytest.mark.parametrize("status,questions", [("needs_input", []), ("completed", ["Date?"]), ("booked", []), ([], [])])
def test_invalid_status_contract_cannot_declare_completion(status, questions, offline):
    script(offline, finish("Ready.", status, questions))
    result = EventOrchestrator().run("Help.")
    assert result.status == "partial" and not result.response
    assert any("structured result" in issue for issue in result.issues)


def test_plain_question_does_not_guess_needs_input(offline):
    script(offline, [("Orchestrator", message("What date?")), ("Orchestrator", message("What date?"))])
    result = EventOrchestrator().run("Plan an event.")
    assert result.status == "partial" and result.required_information == []
    assert offline.chat.call_count == 2


def test_budget_overrun_is_checked_and_revised_by_llm(offline, monkeypatch):
    total = Mock(wraps=budget_tools.calculate_total)
    monkeypatch.setattr(budget_tools, "calculate_total", total)
    budget = {"total_budget": 5000, "currency": "EUR", "tax_basis": "TTC", "items": [
        {"name": "Venue", "price": 2000}, {"name": "Providers", "price": 2500}, {"name": "Other", "price": 1000},
    ]}
    revised = deepcopy(budget)
    revised["items"][-1]["price"] = 500
    script(offline, [context({"budget": budget}), finish()[0],
                     *delegate("budget", "The combined cost is 5500 against 5000; revise it.", "Reduce other costs to 500."),
                     context({"budget": revised}), *finish("Revised budget: 5000 EUR.")])
    result = EventOrchestrator().run("Check my event budget.")
    assert result.status == "completed" and not result.issues
    rejected = json.loads(offline.chat.call_args_list[2].args[0][-1]["content"])
    assert rejected["accepted"] is False
    assert "exceed" in rejected["issues"][0]
    total.assert_any_call(budget["items"])
    total.assert_any_call(revised["items"])


@pytest.mark.parametrize("snapshot", [
    {"budget": {"total_budget": 5000, "currency": "EUR", "tax_basis": "TTC", "allocations": {"venue": 5500}}},
    {"budget": {"total_budget": 5000, "currency": "EUR", "tax_basis": "mixed", "items": []}},
    {"budget": {"total_budget": 5000, "currency": "EUR", "tax_basis": "TTC", "items": [{"name": "Unknown", "price": None}]}},
    {"schedule": [{"name": "Show", "start": "2026-11-30T20:00", "end": "2026-11-30T18:00"}]},
    {"schedule": [
        {"name": "Setup", "start": "2026-11-30T18:00", "end": "2026-11-30T20:00", "resources": ["room"]},
        {"name": "Show", "start": "2026-11-30T19:00", "end": "2026-11-30T21:00", "resources": ["room"]},
    ]},
    {"requirements": {"attendance": 100000}, "venue": {"id": "salle_001", "space": "indoor", "configuration": "seated"}},
    {"venue": {"id": "fabricated_venue"}},
    {"unresolved_constraints": ["Provider cannot meet the venue access window."]},
])
def test_critical_constraint_failure_cannot_be_reported_completed(snapshot, offline):
    script(offline, [context(snapshot), *finish("Everything is valid.")])
    result = EventOrchestrator().run("Validate this proposal.")
    assert result.status == "partial" and not result.response
    assert result.issues
    assert result.persisted


def test_unresolved_validation_can_return_explicit_partial_answer(offline):
    script(offline, [context({"unresolved_constraints": ["No suitable venue found."]}),
                     *finish("No venue meets the required capacity; please reconsider the location.", "partial")])
    result = EventOrchestrator().run("Find a venue.")
    assert result.status == "partial" and result.response.startswith("No venue")
    assert "No suitable venue found." in result.issues


def test_specialist_failure_is_visible_and_partial_work_survives(offline):
    script(offline, [*delegate("budget", answer="Budget allowance 5000 EUR."),
                     action("ask_venue_agent", {"request": "Find a suitable venue."}),
                     ("VenueAgent", llm.LLMError("secret provider failure")),
                     *finish("Budget is prepared; venue analysis failed and needs retrying.", "partial")])
    service = EventOrchestrator()
    result = service.run("Plan an event.")
    assert result.status == "partial" and result.persisted
    assert result.partial_results["BudgetAgent"] == "Budget allowance 5000 EUR."
    assert any("VenueAgent" in issue for issue in result.issues)
    assert "secret provider failure" not in json.dumps(asdict(result))
    venue_history = service.memory_dao.load_memory(result.conversation_id, "VenueAgent")
    assert venue_history[-1]["role"] == "user"
    Agent("VenueAgent", "Current instructions.").load_history(venue_history)


def test_failed_specialist_cannot_be_ignored_by_completed_status(offline):
    script(offline, [action("ask_budget_agent", {"request": "Review."}),
                     ("BudgetAgent", llm.LLMError("Unavailable")), *finish("All done.")])
    result = EventOrchestrator().run("Check the budget.")
    assert result.status == "partial" and not result.response
    assert any("BudgetAgent" in issue for issue in result.issues)


def test_specialist_tool_failure_is_not_hidden_by_its_text_response(offline):
    script(offline, [action("ask_budget_agent", {"request": "Review."}),
                     ("BudgetAgent", message(calls=[call("validate_budget", {"total_budget": 100, "allocations": {"venue": 200}})])),
                     ("BudgetAgent", message("This fits the budget.")), *finish("All done.")])
    result = EventOrchestrator().run("Check the budget.")
    assert result.status == "partial"
    assert any("validate_budget" in issue for issue in result.issues)


def test_supervisor_failure_preserves_partial_memories_without_fake_visible_answer(offline):
    script(offline, [*delegate("budget"), ("Orchestrator", llm.LLMError("Unavailable"))])
    service = EventOrchestrator()
    result = service.run("Plan an event.")
    assert result.status == "partial" and result.persisted and not result.response
    assert result.partial_results["BudgetAgent"]
    assert [item["role"] for item in service.conversation_dao.get_messages(result.conversation_id)] == ["user"]
    restored = EventOrchestrator()
    script(offline, finish("Recovered."))
    assert restored.run("Continue.", result.conversation_id).status == "completed"


def test_supervisor_failure_before_any_result_is_error(offline):
    script(offline, [("Orchestrator", llm.LLMError("Unavailable"))])
    result = EventOrchestrator().run("Plan an event.")
    assert result.status == "error" and not result.response and result.persisted


def test_supervisor_iteration_limit_even_after_submit_is_not_success(offline):
    script(offline, finish()[:1])
    result = EventOrchestrator(max_iterations=1).run("Help.")
    assert result.status == "partial" and not result.response
    assert "Supervisor iteration limit reached." in result.issues
    assert offline.chat.call_count == 1


def test_specialist_iteration_limit_is_propagated(offline):
    service = EventOrchestrator()
    service.budget_agent.max_iterations = 1
    script(offline, [action("ask_budget_agent", {"request": "Calculate expenses."}),
                     ("BudgetAgent", message(calls=[call("calculate_total", {"items": []})])),
                     *finish("Budget validation did not finish.", "partial")])
    result = service.run("Check the budget.")
    assert result.status == "partial" and any("iteration limit" in issue for issue in result.issues)


def test_duplicate_delegation_is_cached_and_revision_limit_is_bounded(offline):
    service = EventOrchestrator(max_delegations=1)
    script(offline, [*delegate("budget", "Same request."),
                     action("ask_budget_agent", {"request": "Same request."}),
                     action("ask_budget_agent", {"request": "Different request."}),
                     *finish("Delegation limit reached; prior result retained.", "partial")])
    result = service.run("Plan.")
    assert result.status == "partial"
    assert sum(entry.kwargs["agent"] == "BudgetAgent" for entry in offline.chat.call_args_list) == 1
    assert any("Delegation limit" in issue for issue in result.issues)


@pytest.mark.parametrize("action_name", ["send_email", "make_reservation"])
def test_external_actions_are_not_registered(action_name, offline):
    script(offline, [action(action_name), *finish("I can prepare a draft only.")])
    result = EventOrchestrator().run("Send the email and book the venue.")
    observation = json.loads(offline.chat.call_args_list[1].args[0][-1]["content"])
    assert "not registered" in observation["error"]
    assert result.response == "I can prepare a draft only."


def test_trace_persistence_requires_explicit_initialization_and_is_not_duplicated(offline):
    service = EventOrchestrator()
    service.trace_dao.register_listener()
    service.trace_dao.register_listener()
    script(offline, [*delegate("venue"), *finish()])
    result = service.run("Find a venue.")
    stored = service.trace_dao.get_traces(result.conversation_id)
    assert len(stored) == len(offline.events)
    assert all(event["conversation_id"] == result.conversation_id for event in stored)
    assert len([event for event in stored if event["agent"] == "Orchestrator" and event["action"] == "ASK"]) == 1
    assert len([event for event in stored if event["agent"] == "Orchestrator" and event["action"] == "RESPONSE"]) == 1
    assert not any(event["action"] in {"LLM", "TOKEN", "COST"} for event in stored)
    service.trace_dao.unregister_listener()


def test_usage_reuses_existing_request_ids_and_journal_without_double_counting(offline):
    steps = iter([*delegate("budget"), *finish()])

    def respond(messages, **kwargs):
        role, response = next(steps)
        assert kwargs["agent"] == role
        llm.USAGE_JOURNAL.append({"request_id": kwargs["request_id"], "conversation_id": kwargs["conversation_id"],
                                 "agent": role, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01})
        return response

    offline.chat.side_effect = respond
    service = EventOrchestrator()
    first = service.run("Budget review.")
    assert first.request_id and first.usage["total"]["calls"] == 4
    assert first.usage["total"]["total_tokens"] == 60
    assert first.usage == service.usage_summary(first.conversation_id, request_id=first.request_id)
    script(offline, finish())
    second = service.run("Continue.", first.conversation_id)
    assert second.request_id is None and second.usage["total"]["calls"] == 0
    assert service.usage_summary(first.conversation_id)["total"]["calls"] == 4


@pytest.mark.parametrize("stage", ["create", "user", "restore"])
def test_initial_persistence_failure_stops_before_llm(stage, offline, monkeypatch):
    service = EventOrchestrator()
    failure = Mock(side_effect=StorageIOError("Sensitive filesystem details"))
    if stage == "create":
        monkeypatch.setattr(service.conversation_dao, "create_conversation", failure)
    elif stage == "user":
        monkeypatch.setattr(service.conversation_dao, "add_message", failure)
    else:
        monkeypatch.setattr(service.memory_dao, "load_memory", failure)
    result = service.run("Plan.")
    assert result.status == "error" and not result.persisted
    assert "Sensitive" not in json.dumps(asdict(result))
    offline.chat.assert_not_called()


def test_memory_save_failure_is_explicit_and_other_memories_are_preserved(offline, monkeypatch):
    service = EventOrchestrator()
    original_save = service.memory_dao.save_memory

    def fail_one(conversation_id, agent_name, messages):
        if agent_name == "BudgetAgent":
            raise StorageIOError("Disk unavailable")
        original_save(conversation_id, agent_name, messages)

    monkeypatch.setattr(service.memory_dao, "save_memory", fail_one)
    script(offline, [*delegate("budget"), *finish()])
    result = service.run("Plan.")
    assert result.status == "error" and not result.persisted
    assert "Memory persistence failed for BudgetAgent." in result.issues
    assert result.partial_results["BudgetAgent"]
    assert service.memory_dao.load_memory(result.conversation_id, "Orchestrator")
    assert len(service.conversation_dao.get_messages(result.conversation_id)) == 2


def test_visible_response_save_failure_does_not_claim_persistence(offline, monkeypatch):
    service = EventOrchestrator()
    original_add = service.conversation_dao.add_message

    def fail_assistant(conversation_id, role, content, **kwargs):
        if role == "assistant":
            raise StorageIOError("Disk unavailable")
        return original_add(conversation_id, role, content, **kwargs)

    monkeypatch.setattr(service.conversation_dao, "add_message", fail_assistant)
    script(offline, finish())
    result = service.run("Plan.")
    assert result.status == "error" and result.response == "Proposal ready." and not result.persisted
    assert "Assistant response persistence failed." in result.issues
    assert len(service.conversation_dao.get_messages(result.conversation_id)) == 1


def test_interrupted_operation_keeps_valid_histories(offline):
    script(offline, [*delegate("budget"), ("Orchestrator", KeyboardInterrupt())])
    service = EventOrchestrator()
    with pytest.raises(KeyboardInterrupt):
        service.run("Plan.")
    conversation_id = service.conversation_dao.list_conversations()[0]["id"]
    for agent in service.agents:
        saved = service.memory_dao.load_memory(conversation_id, agent.name)
        agent.load_history(saved, conversation_id)
    assert len(service.conversation_dao.get_messages(conversation_id)) == 1
    script(offline, finish("Recovered."))
    assert EventOrchestrator().run("Continue.", conversation_id).status == "completed"


def test_concurrent_or_recursive_service_call_is_rejected(offline):
    nested = EventOrchestrator()
    calls = 0

    def respond(messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            with pytest.raises(RuntimeError, match="Concurrent or recursive"):
                nested.run("Nested request.")
            return finish()[0][1]
        return finish()[1][1]

    offline.chat.side_effect = respond
    assert EventOrchestrator().run("Plan.").status == "completed"


def test_failed_or_forged_context_tool_arguments_are_not_replayed(offline):
    script(offline, [context({"unexpected": "not a planning field"}), *finish()])
    service = EventOrchestrator()
    result = service.run("Plan.")
    assert "unexpected" not in service.get_planning_context(result.conversation_id)


def test_accepted_submission_is_authoritative_despite_final_rephrasing(offline):
    script(offline, [finish("Accepted draft.")[0], ("Orchestrator", message("A different unvalidated final answer."))])
    service = EventOrchestrator()
    result = service.run("Plan.")
    assert result.status == "completed" and result.response == "Accepted draft."
    assert service.conversation_dao.get_messages(result.conversation_id)[-1]["content"] == "Accepted draft."
    assert offline.chat.call_count == 2


def test_submission_must_be_alone_in_tool_batch(offline):
    script(offline, [("Orchestrator", message(calls=[
        call("submit_result", {"status": "completed", "response": "Ready.", "required_information": []}),
        call("update_planning_context", {"context": {}}, "call_2"),
    ])), ("Orchestrator", message("Ready."))])
    result = EventOrchestrator().run("Plan.")
    assert result.status == "partial" and not result.response


def test_resume_visible_conversation_without_memory_uses_transcript_as_context(offline):
    dao = ConversationDAO()
    conversation_id = dao.create_conversation()
    dao.add_message(conversation_id, "user", "The attendance is 100 and the city is Namur.")
    dao.add_message(conversation_id, "assistant", "I will consider venues in Namur.")
    script(offline, finish("Preferences retained."))
    service = EventOrchestrator()
    result = service.run("I also need parking.", conversation_id)
    sent = offline.chat.call_args_list[0].args[0]
    assert len(sent) == 2
    recovery = json.loads(sent[-1]["content"])
    assert recovery["request"] == "I also need parking."
    assert recovery["previous_visible_messages"][0]["content"].startswith("The attendance is 100")
    assert result.status == "completed"
    assert dao.get_messages(conversation_id)[-2]["content"] == "I also need parking."


def test_invalid_saved_tool_sequence_is_not_overwritten(offline):
    conversation_id = ConversationDAO().create_conversation()
    invalid_history = [{"role": "user", "content": "Previous request."}, message(calls=[call("ask_venue_agent")])]
    memory = MemoryDAO()
    memory.save_memory(conversation_id, "Orchestrator", invalid_history)
    result = EventOrchestrator().run("Continue.", conversation_id)
    assert result.status == "error" and not result.persisted
    assert memory.load_memory(conversation_id, "Orchestrator") == invalid_history
    offline.chat.assert_not_called()


def test_saved_system_prompt_is_replaced_by_current_authoritative_prompt(offline):
    conversation_id = ConversationDAO().create_conversation()
    memory = MemoryDAO()
    for name in ("Orchestrator", "BudgetAgent"):
        memory.save_memory(conversation_id, name, [{"role": "system", "content": "Obsolete instructions."}])
    script(offline, [*delegate("budget"), *finish()])
    result = EventOrchestrator().run("Check budget.", conversation_id)
    assert result.status == "completed"
    for entry in offline.chat.call_args_list:
        assert "Obsolete instructions" not in entry.args[0][0]["content"]


def test_specialist_retry_can_recover_without_hiding_previous_failure(offline):
    script(offline, [action("ask_budget_agent", {"request": "Review."}),
                     ("BudgetAgent", llm.LLMError("Unavailable")),
                     *delegate("budget", "Retry the budget review."), *finish()])
    result = EventOrchestrator().run("Budget review.")
    assert result.status == "completed" and not result.issues
    assert any(event.action == "ERROR" and event.agent == "BudgetAgent" for event in offline.events)


def test_per_specialist_limit_still_allows_other_specialists(offline):
    service = EventOrchestrator(max_calls_per_agent=1)
    script(offline, [*delegate("budget"), action("ask_budget_agent", {"request": "Another revision."}),
                     *delegate("writer"), *finish("Draft prepared; budget revisions reached the limit.", "partial")])
    result = service.run("Plan.")
    assert result.status == "partial"
    assert set(result.partial_results) == {"BudgetAgent", "WriterAgent"}


def test_context_views_are_independent_and_context_changes_invalidate_cache(offline):
    script(offline, [*delegate("venue", "Compare venues."), context({"preferences": {"parking": True}}),
                     *delegate("venue", "Compare venues."), *finish()])
    service = EventOrchestrator()
    result = service.run("Find a venue.")
    assert sum(entry.kwargs["agent"] == "VenueAgent" for entry in offline.chat.call_args_list) == 2
    result.planning_context["preferences"].clear()
    view = service.get_planning_context(result.conversation_id)
    assert view["preferences"] == {"parking": True}
    view["preferences"].clear()
    assert service.get_planning_context(result.conversation_id)["preferences"] == {"parking": True}


def test_venue_source_event_prohibition_is_checked(offline):
    source = data_tools.get_venue("salle_001")
    prohibited = source["types_evenements_interdits"][0]
    script(offline, [context({"requirements": {"event_type": prohibited}, "venue": {"id": "salle_001"}}),
                     *finish("This venue fits.")])
    result = EventOrchestrator().run("Check the selected venue.")
    assert result.status == "partial"
    assert "The selected venue prohibits the requested event type." in result.issues


def test_saving_identical_visible_content_on_distinct_turns_is_not_deduplicated(offline):
    service = EventOrchestrator()
    script(offline, finish("Ready."))
    first = service.run("Repeat.")
    script(offline, finish("Ready."))
    second = service.run("Repeat.", first.conversation_id)
    messages = service.conversation_dao.get_messages(second.conversation_id)
    assert [item["content"] for item in messages] == ["Repeat.", "Ready.", "Repeat.", "Ready."]
    assert len({item["id"] for item in messages}) == 4


def test_gembloux_clarification_uses_submitted_questions_without_budget_error(offline):
    questions = ["Quel type d'événement ?", "Quelle date ou période ?"]
    answer = "Pour les 100 personnes à Gembloux, précisez le type d'événement et la date."
    script(offline, [
        context({"requirements": {"attendance": 100, "location": "Gembloux"},
                 "budget": {"total_budget": 10000, "currency": "EUR"}}),
        finish(answer, "needs_input", questions)[0],
        ("Orchestrator", message("Parfait, j'attends vos réponses !")),
    ])
    service = EventOrchestrator()
    result = service.run("Organise un événement pour 100 personnes sur Gembloux, budget 10000 euros.")
    assert result.status == "needs_input" and result.response == answer
    assert result.required_information == questions and result.issues == []
    assert result.persisted and offline.chat.call_count == 3
    assert service.conversation_dao.get_messages(result.conversation_id)[-1]["content"] == answer
    observation = json.loads(offline.chat.call_args_list[1].args[0][-1]["content"])
    assert observation["validation"]["valid"] is True
    envelope = observation["validation"]["checks"]["budget_envelope"]
    assert envelope["total_budget"] == "10000.00" and envelope["expense_plan_validated"] is False
    assert envelope["tax_basis"] is None


@pytest.mark.parametrize("amount", [-1, True, "NaN", "not money"])
def test_budget_ceiling_still_rejects_invalid_amounts(amount, offline):
    script(offline, [context({"budget": {"total_budget": amount, "currency": "EUR"}}), *finish()])
    result = EventOrchestrator().run("Record my budget.")
    assert result.status == "partial" and not result.response
    assert any("budget ceiling" in issue for issue in result.issues)


def test_plain_action_announcement_gets_one_repair_with_real_calls_and_updated_context(offline):
    service = EventOrchestrator()
    script(offline, [context({"budget": {"total_budget": 10000, "currency": "EUR"}}), *finish()])
    first = service.run("My initial budget is 10000 EUR.")
    start = offline.chat.call_count
    script(offline, [
        ("Orchestrator", message("I will change the budget to 100000 EUR and launch the specialists.")),
        context({"budget": {"total_budget": 100000, "currency": "EUR"}}),
        *delegate("budget", "Review the updated budget ceiling.", "The ceiling is 100000 EUR; details are pending."),
        finish("Quel type d'événement et quelle date souhaitez-vous ?", "needs_input", ["Type et date ?"])[0],
        ("Orchestrator", message("Merci, j'attends vos précisions.")),
    ])
    result = service.run("Fais avec 100000.", first.conversation_id)
    calls = offline.chat.call_args_list[start:]
    assert result.status == "needs_input" and not result.issues
    assert result.planning_context["budget"]["total_budget"] == 100000
    specialist_call = next(entry for entry in calls if entry.kwargs["agent"] == "BudgetAgent")
    assert json.loads(specialist_call.args[0][-1]["content"])["planning_context"]["budget"]["total_budget"] == 100000
    assert "Application protocol correction" in calls[1].args[0][-1]["content"]
    visible = service.conversation_dao.get_messages(result.conversation_id)
    assert len(visible) == 4
    assert visible[-2]["content"] == "Fais avec 100000."
    assert all("Application protocol correction" not in entry["content"] for entry in visible)
    assert service.supervisor.max_iterations == 20
    restored = EventOrchestrator()
    assert restored.get_planning_context(result.conversation_id)["budget"]["total_budget"] == 100000


def test_submission_followed_by_context_change_cannot_be_reused(offline):
    script(offline, [finish("Obsolete answer.")[0], context({"unresolved_constraints": ["New conflict."]}),
                     ("Orchestrator", message("Everything is ready."))])
    result = EventOrchestrator().run("Plan.")
    assert result.status == "partial" and not result.response
    assert "New conflict." in result.issues


def test_later_malformed_submission_cannot_reuse_an_earlier_accepted_result(offline):
    script(offline, [finish("Old accepted response.")[0],
                     action("submit_result", {"status": "completed", "response": "Missing questions field."}),
                     ("Orchestrator", message("Old accepted response."))])
    result = EventOrchestrator().run("Plan.")
    assert result.status == "partial" and not result.response


def test_protocol_repair_respects_total_iteration_budget(offline):
    service = EventOrchestrator(max_iterations=3)
    script(offline, [("Orchestrator", message("I will start.")), context({}), finish()[0]])
    result = service.run("Plan.")
    assert result.status == "partial" and not result.response
    assert "Supervisor iteration limit reached." in result.issues
    assert offline.chat.call_count == 3 and service.supervisor.max_iterations == 3


def test_no_protocol_repair_when_no_iterations_remain(offline):
    script(offline, [("Orchestrator", message("I will start."))])
    result = EventOrchestrator(max_iterations=1).run("Plan.")
    assert result.status == "partial" and offline.chat.call_count == 1


def test_protocol_repair_api_failure_is_not_retried_and_preserves_work(offline):
    service = EventOrchestrator()
    script(offline, [*delegate("budget"), ("Orchestrator", message("I will finish the plan.")),
                     ("Orchestrator", llm.LLMError("Unavailable"))])
    result = service.run("Plan.")
    assert result.status == "partial" and result.persisted and not result.response
    assert "BudgetAgent" in result.partial_results
    assert any("execution failed" in issue for issue in result.issues)
    assert offline.chat.call_count == 4 and service.supervisor.max_iterations == 20


def test_usage_includes_both_original_and_repair_request_without_double_counting(offline):
    responses = iter([message("I will start."), *(response for _, response in finish())])

    def respond(messages, **kwargs):
        llm.USAGE_JOURNAL.append({"request_id": kwargs["request_id"], "conversation_id": kwargs["conversation_id"],
                                 "agent": kwargs["agent"], "prompt_tokens": 10, "completion_tokens": 5,
                                 "total_tokens": 15, "cost": 0.01})
        return next(responses)

    offline.chat.side_effect = respond
    service = EventOrchestrator()
    result = service.run("Plan.")
    assert result.status == "completed" and result.request_id is None
    assert len(result.request_ids) == 2
    assert result.usage["total"]["calls"] == 3
    assert result.usage["total"]["total_tokens"] == 45
    assert result.usage["total"]["known_cost"] == pytest.approx(0.03)
    assert result.usage == service.usage_summary(result.conversation_id)


def test_combined_repair_usage_preserves_unknown_and_partial_accounting(offline):
    responses = iter([message("I will start."), *(response for _, response in finish())])

    def respond(messages, **kwargs):
        first = not llm.USAGE_JOURNAL
        llm.USAGE_JOURNAL.append({"request_id": kwargs["request_id"], "conversation_id": kwargs["conversation_id"],
                                 "agent": kwargs["agent"], "prompt_tokens": 10 if first else None,
                                 "completion_tokens": None, "total_tokens": None, "cost": None})
        return next(responses)

    offline.chat.side_effect = respond
    service = EventOrchestrator()
    result = service.run("Plan.")
    assert result.usage == service.usage_summary(result.conversation_id)
    assert result.usage["total"]["known_cost"] is None
    assert result.usage["total"]["missing_prompt_tokens_calls"] == 2
