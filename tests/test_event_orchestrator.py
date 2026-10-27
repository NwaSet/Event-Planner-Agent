"""Offline coordination, ID persistence and plain final-response integration."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dao import ConversationDAO, MemoryDAO
from dao import json_storage
from dao.json_storage import StorageIOError
from model import llm, trace
from model.agent import Agent
from services import EventOrchestrator
from services import event_orchestrator as service_module


def message(text=None, calls=None, **extra):
    return {"role": "assistant", "content": text, "tool_calls": calls or [], **extra}


def call(name, arguments=None, identifier="call_1"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments or {})}}


def action(name, arguments=None):
    return ("Orchestrator", message(calls=[call(name, arguments)]))


def event(**changes):
    return action("update_event", {"changes": changes})


def selection(identifier="salle_054", option_id=None):
    return json.dumps({"status": "selected", "items": [{"id": identifier, "option_id": option_id}]})


def delegate(role="venue", answer=None, **options):
    return [action(f"ask_{role}_agent", {"request": "Perform your task.", **options}),
            (role.title() + "Agent", message(answer or selection()))]


def final(text="Proposal ready."):
    return ("Orchestrator", message(text))


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(json_storage, "_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(trace, "_listeners", [])
    monkeypatch.setattr(trace, "_console_enabled", False)
    monkeypatch.setattr(llm, "USAGE_JOURNAL", [])
    monkeypatch.setattr(llm, "get_client", Mock(side_effect=AssertionError("No paid calls.")))
    monkeypatch.setattr(llm, "load_dotenv", Mock(side_effect=AssertionError("No credentials.")))
    chat = Mock(side_effect=AssertionError("A scripted response is required."))
    monkeypatch.setattr(llm, "chat", chat)
    return SimpleNamespace(chat=chat, directory=tmp_path / "logs")


def script(offline, steps):
    queue = iter(steps)

    def respond(messages, **kwargs):
        role, response = next(queue)
        assert kwargs["agent"] == role
        if isinstance(response, BaseException):
            raise response
        return response

    offline.chat.side_effect = respond


def test_initialization_removes_submission_and_business_validation_tools(offline):
    service = EventOrchestrator()
    assert all(type(agent) is Agent for agent in service.agents)
    assert len(service.agents) == 6
    assert set(service.supervisor.functions) == {"update_event", "ask_venue_agent", "ask_provider_agent",
                                               "ask_budget_agent", "ask_schedule_agent", "ask_writer_agent"}
    assert not hasattr(service, "_validate_context")
    assert not offline.directory.exists()
    offline.chat.assert_not_called()


def test_normal_final_response_finishes_once_and_is_saved(offline):
    script(offline, [final("Quelle date souhaitez-vous ?")])
    service = EventOrchestrator()
    result = service.run("Organise une soirée.")
    assert result.response == "Quelle date souhaitez-vous ?" and result.status == "completed" and result.persisted
    assert offline.chat.call_count == 1
    assert [m["role"] for m in service.conversation_dao.get_messages(result.conversation_id)] == ["user", "assistant"]


def test_full_no_meal_event_automatically_saves_ids_kit_budget_and_schedule(offline):
    script(offline, [event(type="shatta", city="Namur", radius_km=20, attendance=100, budget=10000, meal=False, indoor_only=True),
        action("ask_venue_agent", {"request": "Find an indoor venue."}),
        ("VenueAgent", message(calls=[call("search_venues", {"city": "Namur", "radius_km": 20, "min_capacity": 100, "space": "indoor"})])),
        ("VenueAgent", message(selection())),
        action("ask_provider_agent", {"request": "Sound and lights.", "category": "technical"}),
        ("ProviderAgent", message(calls=[call("search_providers", {"city": "Namur", "services": ["son", "light"]})])),
        ("ProviderAgent", message(selection("002", 6))),
        action("ask_budget_agent", {"request": "Read selected costs."}),
        ("BudgetAgent", message(calls=[call("get_item", {"category": "technical", "item_id": "002", "option_id": 6})])),
        ("BudgetAgent", message('{"status":"partial","estimated_total":null,"items":[],"missing_information":["Tax basis"]}')),
        *delegate("schedule", '{"status":"needs_input","activities":[],"missing_information":["Date"]}'),
        final("Salle et technique sélectionnées ; taxes et date restent à préciser.")])
    service = EventOrchestrator()
    result = service.run("Soirée shatta à Namur, sans repas, en intérieur.")
    state = EventOrchestrator().get_planning_context(result.conversation_id)
    assert result.persisted and result.response
    assert state["event"]["meal"] is False and state["event"]["indoor_only"] is True
    assert state["selections"]["venue"] == [{"id": "salle_054", "option_id": None}]
    assert state["selections"]["technical"] == [{"id": "002", "option_id": 6}]
    assert state["budget"]["estimated_total"] is None and state["schedule"]["activities"] == []
    for entry in offline.chat.call_args_list:
        if entry.kwargs["agent"] == "ProviderAgent":
            assert "search_caterers" not in [t["function"]["name"] for t in entry.kwargs["tools"]]
        if entry.kwargs["agent"] == "Orchestrator":
            assert "prix_estime_eur_ht" not in json.dumps(entry.args[0])


def test_summary_next_turn_uses_stored_selections_and_no_searches(offline):
    service = EventOrchestrator()
    script(offline, [event(city="Namur"), *delegate(), final()])
    first = service.run("Find a venue.")
    before = service.venue_agent.get_history(first.conversation_id)
    restored = EventOrchestrator()
    script(offline, [action("ask_writer_agent", {"request": "Summarize the selection."}),
        ("WriterAgent", message(calls=[call("get_item", {"category": "venue", "item_id": "salle_054"})])),
        ("WriterAgent", message("La salle sélectionnée est salle_054.")), final("La salle sélectionnée est salle_054.")])
    offline.chat.reset_mock()
    second = restored.run("Fais une synthèse.", first.conversation_id)
    assert second.status == "completed"
    assert {c.kwargs["agent"] for c in offline.chat.call_args_list} == {"Orchestrator", "WriterAgent"}
    assert restored.venue_agent.get_history(first.conversation_id) == before
    assert second.planning_context["summary"] == "La salle sélectionnée est salle_054."


def test_same_constraints_reuse_completed_work_across_restart_even_with_rephrased_task(offline):
    service = EventOrchestrator()
    script(offline, [event(city="Namur"), *delegate(), final()])
    first = service.run("Find a room.")
    restored = EventOrchestrator()
    script(offline, [action("ask_venue_agent", {"request": "Please review these same venues again."}), final()])
    offline.chat.reset_mock()
    result = restored.run("What was the selection?", first.conversation_id)
    assert result.status == "completed" and result.delegations == {}
    assert offline.chat.call_count == 2
    assert json.loads(offline.chat.call_args_list[-1].args[0][-1]["content"])["reused"] is True


def test_replacement_requires_reason_or_changed_constraints_and_preserves_other_fields(offline):
    service = EventOrchestrator()
    script(offline, [event(city="Namur", meal=False), *delegate(), final()])
    first = service.run("Plan.")
    script(offline, [*delegate(answer=selection("salle_032"), refresh_reason="User requested another venue."), final()])
    second = service.run("Change the venue.", first.conversation_id)
    assert second.planning_context["selections"]["venue"][0]["id"] == "salle_032"
    script(offline, [event(attendance=150), *delegate(answer=selection("salle_054")), final()])
    third = service.run("Now 150 guests.", first.conversation_id)
    assert third.delegations == {"VenueAgent": 1}
    assert third.planning_context["event"] == {"city": "Namur", "meal": False, "attendance": 150}


def test_changed_constraints_mark_saved_results_stale_without_deleting_them(offline):
    service = EventOrchestrator()
    script(offline, [event(attendance=100), *delegate(), final()])
    first = service.run("Plan.")
    script(offline, [event(attendance=500), final("The saved choice needs review.")])
    second = service.run("500 guests now.", first.conversation_id)
    assert second.planning_context["tasks"]["venue:venue"]["stale"] is True
    assert second.planning_context["selections"]["venue"][0]["id"] == "salle_054"


@pytest.mark.parametrize("category", ["catering", "bar", "artists"])
def test_no_meal_or_unsupported_category_does_not_call_provider(offline, category):
    script(offline, [event(meal=False), action("ask_provider_agent", {"request": "Find services.", "category": category}), final("No matching service catalogue.")])
    result = EventOrchestrator().run("No meal; drinks only.")
    assert result.persisted and not result.delegations
    assert all(c.kwargs["agent"] == "Orchestrator" for c in offline.chat.call_args_list)
    assert result.planning_context["selections"][category] == []
    assert result.planning_context["tasks"][f"provider:{category}"]["status"] == "not_found"


def test_failed_replacement_invalidates_cached_summary(offline):
    service = EventOrchestrator()
    script(offline, [*delegate(), *delegate("writer", "Old summary."), final()])
    first = service.run("Plan and summarize.")
    script(offline, [*delegate(answer=selection("missing"), refresh_reason="User requests a new venue."),
                     *delegate("writer", "Previous venue remains; replacement failed."), final("Replacement failed.")])
    second = service.run("Replace and summarize.", first.conversation_id)
    assert second.delegations == {"VenueAgent": 1, "WriterAgent": 1}
    assert second.planning_context["summary"] == "Previous venue remains; replacement failed."


def test_catering_selection_retains_parent_and_formula_ids(offline):
    script(offline, [event(meal=True), *delegate("provider", selection("traiteur_001", "formule_02"), category="catering"), final()])
    service = EventOrchestrator()
    result = service.run("Choose a buffet.")
    assert result.status == "completed"
    assert result.planning_context["selections"]["catering"][0] == {"id": "traiteur_001", "option_id": "formule_02"}
    script(offline, [event(meal=False), final("Meal removed.")])
    result = service.run("No meal anymore.", result.conversation_id)
    assert "catering" not in result.planning_context["selections"]


@pytest.mark.parametrize("answer", [selection("does_not_exist"), '{"status":"selected","items":[]}',
    '{"status":"selected","items":[{"id":2}]}', '{"status":"selected",', 'No structured selection.',
    '{"status":"error","items":[]}', '[1,2,3]'])
def test_invalid_specialist_results_do_not_corrupt_previous_selection(offline, answer):
    service = EventOrchestrator()
    script(offline, [*delegate(), final()])
    first = service.run("Select a venue.")
    script(offline, [*delegate(answer=answer, refresh_reason="User requested a replacement."), final("Previous selection remains available.")])
    result = service.run("Replace it.", first.conversation_id)
    assert result.status == "partial"
    assert result.planning_context["selections"] == first.planning_context["selections"]
    assert result.planning_context["tasks"]["venue:venue"]["stale"]


def test_wrong_kit_id_rejected_but_domain_decisions_not_revalidated(offline):
    script(offline, [event(attendance=999999, budget=1), *delegate(),
                     *delegate("provider", selection("002", 99999)), final("Partial proposal.")])
    result = EventOrchestrator().run("Plan.")
    # Domain decisions belong to VenueAgent, even if a fake LLM picked a poor venue.
    assert result.planning_context["selections"]["venue"][0]["id"] == "salle_054"
    assert "technical" not in result.planning_context["selections"]
    assert result.status == "partial"


def test_no_match_is_saved_and_reused(offline):
    service = EventOrchestrator()
    script(offline, [*delegate(answer='{"status":"not_found","items":[]}'), final("No suitable venue.")])
    first = service.run("Find an option.")
    assert first.status == "completed" and first.planning_context["selections"]["venue"] == []
    script(offline, [action("ask_venue_agent", {"request": "Same search."}), final("Still no match.")])
    assert not service.run("Same constraints.", first.conversation_id).delegations


@pytest.mark.parametrize("options", [{"max_iterations": 0}, {"max_calls_per_agent": True}, {"max_delegations": -1}])
def test_invalid_limits(options):
    with pytest.raises(ValueError):
        EventOrchestrator(**options)


def test_delegation_limit_and_supervisor_limit_remain_bounded(offline):
    service = EventOrchestrator(max_calls_per_agent=1)
    script(offline, [*delegate(), action("ask_venue_agent", {"request": "Again.", "refresh_reason": "Another venue."}), final("Partial.")])
    result = service.run("Plan.")
    assert result.delegations == {"VenueAgent": 1} and result.status == "partial"
    assert any("limit" in issue for issue in result.issues)
    service = EventOrchestrator(max_iterations=1)
    script(offline, [event(city="Namur")])
    assert service.run("Plan.").status == "partial"


@pytest.mark.parametrize("response", [message("Budget total is", finish_reason="length"),
                                       message(None, finish_reason="length"), message("{\"tool_calls\": []}"),
                                       message(None, calls=[{"id": "bad", "type": "function", "function": {}}])])
def test_incomplete_or_malformed_supervisor_response_never_becomes_success(offline, response):
    offline.chat.return_value, offline.chat.side_effect = response, None
    result = EventOrchestrator().run("Plan.")
    assert result.status in {"partial", "error"} and result.issues
    offline.chat.assert_called_once()


def test_plain_tool_announcement_does_not_execute_tools_or_invent_state(offline):
    script(offline, [final("I will call ask_venue_agent next.")])
    result = EventOrchestrator().run("Plan.")
    assert not result.delegations and result.planning_context["selections"] == {}
    assert offline.chat.call_count == 1


def test_partial_saved_selection_survives_supervisor_api_failure(offline):
    script(offline, [*delegate(), ("Orchestrator", llm.LLMError("Provider unavailable"))])
    result = EventOrchestrator().run("Plan.")
    assert result.status == "partial" and result.persisted and "salle_054" in result.response
    assert EventOrchestrator().get_planning_context(result.conversation_id)["selections"]["venue"]


def test_budget_only_partial_work_is_visible_after_supervisor_failure(offline):
    script(offline, [*delegate("budget", '{"status":"completed","estimated_total":1200}'),
                     ("Orchestrator", llm.LLMError("Unavailable"))])
    result = EventOrchestrator().run("Estimate the budget.")
    assert result.status == "partial" and result.persisted and "budget" in result.response
    assert result.planning_context["budget"]["estimated_total"] == 1200


def test_selected_state_is_written_before_supervisor_continues(offline):
    service = EventOrchestrator()
    script(offline, [*delegate(), final()])
    respond = offline.chat.side_effect

    def check(messages, **kwargs):
        if kwargs["agent"] == "Orchestrator" and messages[-1]["role"] == "tool":
            state = service.conversation_dao.get_conversation(kwargs["conversation_id"])["event_state"]
            assert state["selections"]["venue"][0]["id"] == "salle_054"
        return respond(messages, **kwargs)

    offline.chat.side_effect = check
    assert service.run("Plan.").persisted


def test_persistence_failure_before_paid_work_stops(offline, monkeypatch):
    service = EventOrchestrator()
    monkeypatch.setattr(service.conversation_dao, "save_event_state", Mock(side_effect=StorageIOError("Cannot write")))
    result = service.run("Plan.")
    assert not result.persisted and result.status == "error"
    offline.chat.assert_not_called()


def test_unknown_conversation_and_concurrent_run_rejected(offline):
    service = EventOrchestrator()
    with pytest.raises(LookupError):
        service.run("Plan.", "conv_unknown")
    with service_module._RUN_LOCK:
        with pytest.raises(RuntimeError):
            service.run("Plan.")
    offline.chat.assert_not_called()


def test_conversations_are_isolated_and_state_reading_makes_no_llm_call(offline):
    service = EventOrchestrator()
    script(offline, [event(city="Namur"), *delegate(), final(), final("Other event.")])
    first, second = service.run("Plan."), service.run("Other event.")
    assert second.planning_context["event"] == {} and second.planning_context["selections"] == {}
    count = offline.chat.call_count
    state = service.get_planning_context(first.conversation_id)
    state["event"]["city"] = "Changed outside"
    assert service.get_planning_context(first.conversation_id)["event"]["city"] == "Namur"
    assert offline.chat.call_count == count


def test_legacy_structured_selection_is_recovered_without_a_new_search(offline):
    dao = ConversationDAO()
    cid = dao.create_conversation()
    MemoryDAO().save_memory(cid, "VenueAgent", [message('```json\n{"status":"completed","selections":[{"id":"salle_054","name":"Old copied data"}]}\n```'),
                                               message("The model returned an empty response; the request could not be completed.")])
    state = EventOrchestrator().get_planning_context(cid)
    assert state["selections"]["venue"] == [{"id": "salle_054", "option_id": None}]
    offline.chat.assert_not_called()


def test_usage_and_request_id_include_specialists_once(offline):
    service = EventOrchestrator()
    script(offline, [*delegate(), final()])
    respond = offline.chat.side_effect

    def measured(messages, **kwargs):
        llm.USAGE_JOURNAL.append({"agent": kwargs["agent"], "conversation_id": kwargs["conversation_id"],
                                 "request_id": kwargs["request_id"], "prompt_tokens": 10, "completion_tokens": 5,
                                 "total_tokens": 15, "cost": None})
        return respond(messages, **kwargs)

    offline.chat.side_effect = measured
    result = service.run("Plan.")
    assert len(result.request_ids) == 1
    assert result.usage["total"]["calls"] == 3 and result.usage["total"]["total_tokens"] == 45
    assert result.usage["agents"]["VenueAgent"]["calls"] == 1
    assert result.usage["total"]["known_cost"] is None
