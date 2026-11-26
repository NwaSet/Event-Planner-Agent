"""Régression 52923e36 : prestation ajoutée après une allocation réservée à la salle."""

import copy
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from controller.event_controller import continue_conversation
from dao.conversation_dao import create_conversation, save_conversation, load_conversation
from model import agent as agent_module, event_state, trace
from model.agents import create_all_agents
from model.event_orchestrator import create_orchestrator
from model.event_state import EventRun


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 26)
    monkeypatch.setattr(event_state, "date", Today)
    monkeypatch.setattr(trace, "tracer", Mock())
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("LLM non simulé")))


def previous_event(*, failed_provider=False):
    run = EventRun("Organiser une soirée à Namur pour 150 personnes, budget 10000 euros", interactive=True)
    run.remember({"city": "Namur", "guests": 150, "budget_total": 10000, "rental_period": "soiree"},
                 ["planning", "budget", "venue"])
    run.event_data.update(planning={"start": "2026-12-05T19:00", "end": "2026-12-05T23:00"},
                          budget={"venue": 1, "catering": 0, "provider": 0}, venue={"id": "salle_054"})
    for task in ("planning", "budget", "venue"):
        run.event_data["tasks"][task] = {"status": "success"}
    # Les sauvegardes réelles antérieures ne décrivent pas la portée de l'allocation.
    run.event_data.pop("budget_categories", None)
    if failed_provider:
        run.event_data["tasks"]["provider"] = {"status": "no_match", "retryable": False,
                                                "error": "Aucune offre ne respecte toutes les contraintes."}
    return run.event_data


@pytest.mark.parametrize("failed_provider", [False, True])
@pytest.mark.parametrize("announced", [False, True])
def test_added_provider_requires_new_allocation_before_search(failed_provider, announced):
    previous = previous_event(failed_provider=failed_provider)
    run = EventRun("trouve le prestataire technique", event_data=previous, interactive=True)
    _, _, _, decision = create_all_agents()
    result = run.delegate(decision, "Prestataire technique", "provider", tasks=["provider"] if announced else None)
    assert result["status"] == "not_ready" and result["requires"] == "budget"
    assert run.event_data["budget"] is None
    assert run.event_data["tasks"]["budget"]["status"] == "pending"
    assert run.event_data["requirements"]["budget_total"] == 10000
    assert run.event_data["venue"] == previous["venue"] and run.event_data["planning"] == previous["planning"]
    assert run.counters["delegations"] == 0
    agent_module.chat.assert_not_called()


def tool(name, **arguments):
    return SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id=name,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))])


def test_saved_event_reallocates_once_then_decision_really_selects_provider(tmp_path, monkeypatch):
    previous = previous_event(failed_provider=True)
    conversation = create_conversation(previous["request"])
    conversation["event_data"] = previous
    save_conversation(conversation, tmp_path)
    agents = []
    supervisor_steps = iter([
        tool("decision_making_agent", category="provider", demande="Prestataire technique", tasks=["provider"]),
        tool("budgetary_agent", demande="Répartis le même budget pour salle et prestataire", tasks=["budget"]),
        tool("decision_making_agent", category="provider", demande="Prestataire technique", tasks=["provider"]),
    ])
    def simulate(messages, agent=None, **kwargs):
        agents.append(agent)
        if agent == "Supervisor":
            return next(supervisor_steps)
        payload = json.loads(messages[1]["content"])
        if agent == "budgetary agent":
            assert payload["categories"] == ["venue", "provider"]
            assert payload["requirements"]["budget_total"] == 10000
            return SimpleNamespace(content=json.dumps({"venue": .6, "catering": 0, "provider": .4}), tool_calls=None)
        assert agent == "decision making agent" and payload["task"] == "provider"
        assert payload["filters"]["max_budget"] == 4000
        row = payload["search"]["candidates"][0]
        return SimpleNamespace(content=json.dumps({"id": row["id"], "kit_id": row["kits"][0]["id"]}), tool_calls=None)
    monkeypatch.setattr(agent_module, "chat", simulate)
    updated = continue_conversation(load_conversation(conversation["conversation_id"], tmp_path),
                                    "trouve le prestataire technique", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert updated["event_data"]["tasks"]["provider"]["status"] == "success"
    assert updated["event_data"]["planning"] == previous["planning"]
    assert updated["event_data"]["venue"] == previous["venue"]
    assert updated["event_data"]["requirements"]["budget_total"] == 10000
    assert agents == ["Supervisor", "Supervisor", "budgetary agent", "Supervisor", "decision making agent"]
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert load_conversation(updated["conversation_id"], tmp_path) == updated


def test_explicit_zero_allocation_for_known_category_does_not_reallocate_in_a_loop():
    previous = previous_event()
    previous["tasks"]["provider"] = {"status": "pending"}
    previous["budget_categories"] = ["venue", "provider"]
    run = EventRun("Prestataire technique", event_data=previous, interactive=True)
    _, _, _, decision = create_all_agents()
    first = run.delegate(decision, "Prestataire", "provider")
    assert first["status"] == "no_match"
    assert run.delegate(decision, "Même recherche", "provider")["status"] == "reused"
    assert run.event_data["budget"] == previous["budget"]
    assert run.event_data["tasks"]["budget"]["status"] == "success"
    agent_module.chat.assert_not_called()


def test_no_global_ceiling_does_not_trigger_budgetary(monkeypatch):
    previous = previous_event()
    previous["requirements"].pop("budget_total")
    previous["budget"] = None
    previous["tasks"].pop("budget")
    run = EventRun("Prestataire technique", event_data=previous, interactive=True)
    _, _, _, decision = create_all_agents()
    def choose(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        row = payload["search"]["candidates"][0]
        return SimpleNamespace(content=json.dumps({"id": row["id"], "kit_id": row["kits"][0]["id"]}), tool_calls=None)
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=choose))
    assert run.delegate(decision, "Prestataire", "provider")["status"] == "success"
    assert "budget" not in run.event_data["tasks"]
    assert agent_module.chat.call_count == 1


def test_covered_allocation_and_successful_service_are_reused_without_more_calls():
    previous = previous_event()
    previous["budget"] = {"venue": .6, "catering": 0, "provider": .4}
    previous["budget_categories"] = ["venue", "provider"]
    # La salle 054 autorise le prestataire 005 (catalogue conservé).
    previous["providers"] = [{"id": "005", "kit_id": 1}]
    previous["tasks"]["provider"] = {"status": "success"}
    run = EventRun("Prestataire technique", event_data=previous, interactive=True)
    _, _, _, decision = create_all_agents()
    assert run.delegate(decision, "Prestataire", "provider")["status"] == "reused"
    assert run.event_data["budget"] == previous["budget"]
    assert run.event_data["tasks"]["budget"]["status"] == "success"
    agent_module.chat.assert_not_called()


def test_new_budget_scope_archives_old_approval_once_and_keeps_exact_document():
    previous = previous_event()
    previous["plan_approval"] = {"approved": True, "plan": {}, "content": "Plan approuvé", "revision": 1}
    previous["documents"] = [{"content": " \r\nAncien email.  ", "approved": True, "revision": 1}]
    run = EventRun("Ajoute le prestataire technique", event_data=previous, interactive=True)
    _, _, _, decision = create_all_agents()
    assert run.delegate(decision, "Prestataire", "provider")["status"] == "not_ready"
    revision = run.event_data["revision"]
    assert run.event_data["plan_approval"] is None and run.event_data["documents"] == []
    assert run.event_data["document_archive"] == previous["documents"]
    assert run.delegate(decision, "Prestataire", "provider")["status"] == "not_ready"
    assert run.event_data["revision"] == revision
    assert len(run.event_data["approval_archive"]) == len(run.event_data["document_archive"]) == 1
