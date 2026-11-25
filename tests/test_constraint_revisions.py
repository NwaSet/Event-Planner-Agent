"""Régressions des traces 899ca971, 4e72bf9f et 559f5934 ; aucun service réel."""

import copy
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from model import agent as agent_module, event_state, trace
from model.agents import create_all_agents
from model.event_state import EventRun
from model.event_orchestrator import create_orchestrator
from controller.event_controller import continue_conversation
from dao.conversation_dao import create_conversation, save_conversation, load_conversation


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 25)
    monkeypatch.setattr(event_state, "date", Today)
    monkeypatch.setattr(trace, "tracer", Mock())
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("LLM non simulé")))


def message(content=None, calls=None):
    return SimpleNamespace(content=content, tool_calls=calls)


def select_first(messages, **kwargs):
    context = json.loads(messages[1]["content"])
    row = context["search"]["candidates"][0]
    result = {"id": row["id"]}
    if context["task"] == "provider":
        result["kit_id"] = row["kits"][0]["id"]
    return message(json.dumps(result))


def failed_event(request="Modifie l'événement", task="venue"):
    run = EventRun(request, interactive=True)
    run.remember({"city": "Namur", "guests": 100, "location_scope": "city", "budget_total": 5000,
                  "planning": {"start": "2026-11-27T19:00", "end": "2026-11-27T23:00"}},
                 ["venue", "provider", "budget"])
    run.event_data["budget"] = {"venue": .55, "catering": 0, "provider": .45}
    run.event_data["tasks"]["budget"] = {"status": "success"}
    run.event_data["tasks"][task] = {"status": "no_match", "retryable": False, "error": "Recherche précédente épuisée"}
    run.attempts[task] = {"count": 2, "input": {"ancienne": "recherche"}}
    return run


@pytest.mark.parametrize("status", ["error", "no_match"])
@pytest.mark.parametrize("change", [{"location_scope": "province_fallback"},
                                    {"venue_filters": {"max_budget": 2000}}, {"budget_total": 6000}])
def test_explicit_revision_reopens_affected_failed_task_only(status, change):
    run = failed_event()
    run.event_data["tasks"]["venue"]["status"] = status
    untouched = copy.deepcopy(run.event_data["tasks"]["planning"])
    run.revise_event(requirements=change)
    assert all(run.event_data["requirements"][key] == value for key, value in change.items())
    assert run.event_data["tasks"]["venue"]["status"] == "pending"
    assert "venue" not in run.attempts
    assert run.event_data["tasks"]["planning"] == untouched
    assert run.event_data["tasks"]["provider"] == {"status": "pending"}


def test_logged_scope_conflict_does_not_poison_task_and_explicit_revision_can_execute(monkeypatch):
    run = failed_event()
    _, _, _, decision = create_all_agents()
    before = copy.deepcopy(run.event_data)
    outcome = run.delegate(decision, "Salle dans la province", "venue",
                           requirements={"location_scope": "province_fallback"})
    assert outcome["status"] == "requires_revision"
    assert run.event_data == before
    run.revise_event(requirements={"location_scope": "province_fallback"})
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=select_first))
    assert run.delegate(decision, "Salle dans la province", "venue")["status"] == "success"
    assert agent_module.chat.call_count == 1
    assert run.delegate(decision, "Même recherche", "venue")["status"] == "reused"
    assert agent_module.chat.call_count == 1


def test_temporary_venue_ceiling_can_change_without_becoming_a_persistent_constraint(monkeypatch):
    run = EventRun("Cherche une salle", interactive=True)
    run.remember({"city": "Namur", "guests": 100}, ["venue"])
    _, _, _, decision = create_all_agents()
    assert run.delegate(decision, "Essai trop bas", "venue", requirements={"venue_filters": {"max_budget": 1}})["status"] == "no_match"
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=select_first))
    assert run.delegate(decision, "Plafond de recherche corrigé", "venue",
                        requirements={"venue_filters": {"max_budget": 2750}})["status"] == "success"
    assert "max_budget" not in run.event_data["requirements"].get("venue_filters", {})
    restored = EventRun("Autre question", event_data=run.event_data, interactive=True)
    assert "max_budget" not in restored.context("venue")["filters"]


def test_local_provider_cap_in_logged_delegation_keeps_global_budget(monkeypatch):
    run = failed_event("trouve le prestataire sur namur pour 2500 euros", task="provider")
    run = EventRun(run.turn_request, event_data=run.event_data, interactive=True)
    before = copy.deepcopy(run.event_data)
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=select_first))
    outcome = run.delegate(decision, "trouve le prestataire sur namur pour 2500 euros", "provider",
                           requirements={"budget_total": 2500, "provider_filters": {"city": "Namur", "max_budget": 2500}},
                           tasks=["provider"])
    assert outcome["status"] == "success"
    assert run.event_data["requirements"]["budget_total"] == 5000
    assert run.event_data["budget"] == before["budget"]
    for task in ("venue", "planning", "budget"):
        assert run.event_data["tasks"][task] == before["tasks"][task]
    assert agent_module.chat.call_count == 1


def test_logged_misrouted_provider_budget_revision_does_not_reallocate_event():
    run = failed_event("trouve le prestataire sur namur pour 2500 euros", task="provider")
    before = copy.deepcopy(run.event_data)
    run.revise_event(requirements={"budget_total": 2500}, replace=[], remove=[], unset=[])
    assert run.event_data["requirements"]["budget_total"] == 5000
    assert run.event_data["requirements"]["provider_filters"]["max_budget"] == 2500
    assert run.event_data["tasks"]["provider"]["status"] == "pending"
    assert run.event_data["budget"] == before["budget"]
    for task in ("venue", "planning", "budget"):
        assert run.event_data["tasks"][task] == before["tasks"][task]


def test_unchanged_revision_does_not_reset_attempts_or_reopen_failures():
    run = failed_event()
    before = copy.deepcopy(run.event_data)
    attempts = copy.deepcopy(run.attempts)
    assert run.revise_event(requirements={"location_scope": "city"})["status"] == "unchanged"
    assert run.event_data == before and run.attempts == attempts


def test_reopened_saved_failure_executes_only_affected_specialist(tmp_path, monkeypatch):
    run = failed_event()
    run.event_data["providers"] = [{"id": "005", "kit_id": 1}]
    run.event_data["tasks"]["provider"] = {"status": "success"}
    original = copy.deepcopy(run.event_data)
    conversation = create_conversation("Salle à Namur")
    conversation["event_data"] = run.event_data
    save_conversation(conversation, tmp_path)
    responses = iter([
        ("revise_event", {"requirements": {"location_scope": "province_fallback"}}),
        ("decision_making_agent", {"category": "venue", "demande": "Salle", "tasks": ["venue"]}),
    ])
    agents = []

    def chat(messages, agent=None, **kwargs):
        agents.append(agent)
        if agent != "Supervisor":
            return select_first(messages)
        name, arguments = next(responses)
        return message(calls=[SimpleNamespace(id=name, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))])

    monkeypatch.setattr(agent_module, "chat", chat)
    updated = continue_conversation(load_conversation(conversation["conversation_id"], tmp_path),
                                    "Tu peux chercher dans la province", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert updated["event_data"]["tasks"]["venue"]["status"] == "success"
    for field in ("planning", "budget", "providers"):
        assert updated["event_data"][field] == original[field]
    assert agents == ["Supervisor", "Supervisor", "decision making agent"]
    assert updated["conversation_id"] == conversation["conversation_id"]
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert load_conversation(updated["conversation_id"], tmp_path) == updated


def test_temporary_filters_cannot_bypass_persistent_ceiling_or_two_attempt_limit(monkeypatch):
    run = EventRun("Salle à Namur", interactive=True)
    run.remember({"guests": 100}, ["venue"])
    run.revise_event(requirements={"venue_filters": {"max_budget": 3000}})
    _, _, _, decision = create_all_agents()
    for cap in (1, 2):
        assert run.delegate(decision, "Recherche", "venue", requirements={"venue_filters": {"max_budget": cap}})["status"] == "no_match"
    assert run.delegate(decision, "Troisième essai", "venue", requirements={"venue_filters": {"max_budget": 5000}})["status"] == "reused"
    assert run.context("venue")["filters"]["max_budget"] == 3000
    agent_module.chat.assert_not_called()
    run.revise_event(requirements={"venue_filters": {"max_budget": 4000}})
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=select_first))
    assert run.delegate(decision, "Après modification explicite", "venue")["status"] == "success"
    assert agent_module.chat.call_count == 1


@pytest.mark.parametrize("user_request", ["Le budget global passe à 4000 euros pour salle et prestataire",
                                    "Budget total : 4000 euros, prestataire compris"])
def test_explicit_global_budget_still_changes(user_request):
    run = failed_event(user_request)
    run.revise_event(requirements={"budget_total": 4000})
    assert run.event_data["requirements"]["budget_total"] == 4000
    assert run.event_data["tasks"]["budget"]["status"] == "pending"
    assert run.event_data["budget"] is None


def test_local_provider_request_does_not_launch_budgetary():
    run = failed_event("trouve le prestataire sur namur pour 2500 euros")
    before = copy.deepcopy(run.event_data)
    budget, _, _, _ = create_all_agents()
    outcome = run.delegate(budget, "répartir le budget de 2500 euros pour la soirée", "budget",
                           requirements={"budget_total": 2500}, tasks=["budget"])
    assert outcome["status"] == "not_required" and run.event_data == before
    agent_module.chat.assert_not_called()


def test_correct_provider_delegation_can_repeat_unchanged_global_budget(monkeypatch):
    old = failed_event("trouve le prestataire sur namur pour 2500 euros", task="provider")
    run = EventRun(old.turn_request, event_data=old.event_data, interactive=True)
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=select_first))
    assert run.delegate(decision, "Prestataire à Namur", "provider", requirements={
        "budget_total": 5000, "provider_filters": {"city": "Namur", "max_budget": 2500}})["status"] == "success"
    assert run.event_data["requirements"]["budget_total"] == 5000
    assert run.event_data["tasks"]["budget"]["status"] == "success"


def test_headcount_discovered_in_delegation_is_persistent_but_search_ids_are_not(monkeypatch):
    run = EventRun("Salle à Namur", interactive=True)
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=select_first))
    assert run.delegate(decision, "Salle", "venue", requirements={"venue_filters": {
        "guests": 100, "rental_period": "soiree", "ids": ["salle_010"], "max_budget": 2750}})["status"] == "success"
    restored = EventRun("Autre question", event_data=run.event_data, interactive=True)
    filters = restored.context("venue")["filters"]
    assert filters["guests"] == 100 and filters["rental_period"] == "soiree"
    assert "max_budget" not in filters and "ids" not in filters


def test_unrelated_revision_keeps_failed_task_and_exhausted_attempts():
    run = failed_event()
    before = copy.deepcopy(run.event_data["tasks"]["venue"])
    attempts = copy.deepcopy(run.attempts)
    run.revise_event(requirements={"document_kind": "email"})
    assert run.event_data["tasks"]["venue"] == before
    assert run.attempts == attempts


def test_explicit_scope_revision_discards_failed_temporary_ids_and_budget(monkeypatch):
    run = EventRun("Salle à Namur", interactive=True)
    run.remember({"guests": 100, "location_scope": "city"}, ["venue"])
    _, _, _, decision = create_all_agents()
    assert run.delegate(decision, "Premier essai", "venue", requirements={"venue_filters": {
        "ids": ["inconnu"], "max_budget": 1}})["status"] == "no_match"
    run.revise_event(requirements={"location_scope": "province_fallback"})
    filters = run.context("venue")["filters"]
    assert "ids" not in filters and "max_budget" not in filters
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=select_first))
    assert run.delegate(decision, "Nouvelle recherche", "venue")["status"] == "success"


def test_global_budget_change_in_delegation_requires_explicit_revision_without_losing_state():
    run = failed_event("Le budget global passe à 6000 euros")
    run.scope_declared = False
    before = copy.deepcopy(run.event_data)
    budget, _, _, _ = create_all_agents()
    result = run.delegate(budget, "Répartis 6000 euros", "budget", requirements={"budget_total": 6000}, tasks=["budget"])
    assert result["status"] == "requires_revision"
    assert run.event_data == before and run.scope_declared is False
    run.revise_event(requirements={"budget_total": 6000})
    assert run.event_data["requirements"]["budget_total"] == 6000
    assert run.event_data["tasks"]["budget"]["status"] == "pending"


def test_persistent_search_filter_changes_only_through_explicit_revision():
    run = EventRun("Salle à Namur", interactive=True)
    run.remember({"venue_filters": {"ids": ["salle_010"]}}, ["venue"])
    before = copy.deepcopy(run.event_data)
    _, _, _, decision = create_all_agents()
    result = run.delegate(decision, "Autre salle", "venue", requirements={"venue_filters": {"ids": ["salle_001"]}})
    assert result["status"] == "requires_revision" and run.event_data == before
    run.revise_event(requirements={"venue_filters": {"ids": ["salle_001"]}})
    assert run.context("venue")["filters"]["ids"] == ["salle_001"]


def test_budget_result_reopens_previously_blocked_selection_but_not_planning(monkeypatch):
    run = failed_event(task="provider")
    run.event_data["budget"] = None
    run.event_data["tasks"]["budget"] = {"status": "pending"}
    run.event_data["tasks"]["provider"] = {"status": "blocked", "retryable": False}
    old_planning = copy.deepcopy(run.event_data["planning"])
    budget, _, _, _ = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message(json.dumps({"venue": .55, "catering": 0, "provider": .45}))))
    assert run.delegate(budget, "Répartition corrigée", "budget")["status"] == "success"
    assert run.event_data["tasks"]["provider"]["status"] == "pending"
    assert "provider" not in run.attempts
    assert run.event_data["planning"] == old_planning
