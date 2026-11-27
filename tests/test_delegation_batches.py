"""Lots Supervisor : annoncer le travail valide avant de décider que le tour est terminé."""

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from model import agent as agent_module, event_state, trace
from model.event_orchestrator import create_orchestrator


PLANNING = {"start": "2026-12-05T19:00", "end": "2026-12-05T23:00"}


def call(name, **arguments):
    return SimpleNamespace(id=name, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def message(content=None, *calls):
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 26)
    monkeypatch.setattr(event_state, "date", Today)
    monkeypatch.setattr(trace, "tracer", Mock())
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("LLM non simulé")))


def test_planning_does_not_skip_budgetary_announced_later_in_the_same_batch(monkeypatch):
    seen = []

    def fake(messages, agent=None, **kwargs):
        seen.append(agent)
        if agent == "Supervisor":
            return message(None,
                call("planning_agent", demande="Date", requirements={"budget_total": 10000}, tasks=["planning"]),
                call("budgetary_agent", demande="Budget", tasks=["budget"]))
        if agent == "planning agent":
            return message(json.dumps(PLANNING))
        assert agent == "budgetary agent"
        # Pas de catégorie d'achat encore annoncée : aucune allocation fictive.
        return message(json.dumps({"venue": 0, "catering": 0, "provider": 0}))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    supervisor.run("Planning et budget de 10000 euros", interactive=True)
    assert seen == ["Supervisor", "planning agent", "budgetary agent"]
    assert supervisor.event_data["results"]["planning"] == PLANNING
    assert supervisor.event_data["tasks"]["planning"]["status"] == supervisor.event_data["tasks"]["budget"]["status"] == "success"


def test_batch_scope_reaches_the_selection_after_planning_and_budget(monkeypatch):
    seen = []

    def fake(messages, agent=None, **kwargs):
        seen.append(agent)
        if agent == "Supervisor":
            return message(None,
                call("planning_agent", demande="Date", requirements={"city": "Namur", "guests": 100, "budget_total": 10000}, tasks=["planning"]),
                call("budgetary_agent", demande="Budget", tasks=["budget"]),
                call("decision_making_agent", demande="Salle", category="venue", tasks=["venue"]))
        if agent == "planning agent":
            return message(json.dumps(PLANNING))
        if agent == "budgetary agent":
            payload = json.loads(messages[1]["content"])
            assert payload["categories"] == ["venue"]
            return message(json.dumps({"venue": 1, "catering": 0, "provider": 0}))
        assert agent == "decision making agent"
        payload = json.loads(messages[1]["content"])
        return message(json.dumps({"id": payload["search"]["candidates"][0]["id"]}))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    supervisor.run("Salle à Namur pour 100 personnes, 10000 euros", interactive=True)
    assert seen == ["Supervisor", "planning agent", "budgetary agent", "decision making agent"]
    assert supervisor.event_data["results"]["venue"]
    assert all(item["status"] == "success" for item in supervisor.event_data["tasks"].values())


def test_without_announced_tasks_supervisor_can_keep_delegating_in_next_react_turn(monkeypatch):
    seen = []

    def fake(messages, agent=None, **kwargs):
        seen.append(agent)
        if agent == "Supervisor":
            if seen.count("Supervisor") == 1:
                return message(None, call("planning_agent", demande="Date"))
            return message("La date est proposée.")
        return message(json.dumps(PLANNING))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    supervisor.run("Une date", interactive=True)
    assert seen == ["Supervisor", "planning agent", "Supervisor"]


@pytest.mark.parametrize("category", [None, "unknown", ["venue"]])
def test_invalid_decision_category_does_not_announce_or_execute_a_task(monkeypatch, category):
    seen = []

    def fake(messages, agent=None, **kwargs):
        seen.append(agent)
        if agent == "Supervisor":
            arguments = {"demande": "Salle", "tasks": ["venue"]}
            if category is not None:
                arguments["category"] = category
            return message(None, call("decision_making_agent", **arguments),
                           call("planning_agent", demande="Date", tasks=["planning"]))
        assert agent == "planning agent"
        return message(json.dumps(PLANNING))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    supervisor.run("Une date", interactive=True)
    assert set(supervisor.event_data["tasks"]) == {"planning"}
    assert seen == ["Supervisor", "planning agent"]


def test_unrequested_writer_in_batch_is_not_announced_or_executed(monkeypatch):
    seen = []

    def fake(messages, agent=None, **kwargs):
        seen.append(agent)
        if agent == "Supervisor":
            return message(None, call("planning_agent", demande="Date", tasks=["planning", "documents"]),
                           call("writer_agent", demande="Email", tasks=["documents"], requirements={"document_kind": "email"}))
        assert agent == "planning agent"
        return message(json.dumps(PLANNING))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    supervisor.run("Une date uniquement", interactive=True)
    assert set(supervisor.event_data["tasks"]) == {"planning"}
    assert seen == ["Supervisor", "planning agent"]


def test_clarification_still_stops_before_later_delegations(monkeypatch):
    chat = Mock(return_value=message(None, call("clarify", question="Quelle ville ?"),
                                    call("planning_agent", demande="Date", tasks=["planning"])))
    monkeypatch.setattr(agent_module, "chat", chat)
    supervisor = create_orchestrator()
    result = supervisor.run("Un événement", interactive=True)
    assert result == "Quelle ville ?" and chat.call_count == 1
    assert supervisor.event_data["tasks"] == {}
    assert supervisor.event_data["workflow"]["pending_input"]["kind"] == "clarification"


def test_pending_plan_approval_still_stops_before_later_revision(monkeypatch):
    chat = Mock(return_value=message(None,
        call("writer_agent", demande="Brouillon", requirements={"document_kind": "email"}, tasks=["documents"]),
        call("revise_event", requirements={"guests": 150})))
    monkeypatch.setattr(agent_module, "chat", chat)
    supervisor = create_orchestrator()
    supervisor.run("Rédige un email pour un événement de 100 personnes", interactive=True)
    assert chat.call_count == 1
    assert supervisor.event_data["workflow"]["pending_input"]["kind"] == "plan_approval"
    assert supervisor.event_data["requirements"].get("guests") != 150


def test_malformed_tasks_in_one_call_do_not_abort_the_rest_of_the_batch(monkeypatch):
    seen = []

    def fake(messages, agent=None, **kwargs):
        seen.append(agent)
        if agent == "Supervisor":
            return message(None,
                call("budgetary_agent", demande="Budget", tasks=[{"bad": "task"}], requirements={"tasks": ["budget"]}),
                call("planning_agent", demande="Date", tasks=["planning"]))
        assert agent == "planning agent"
        return message(json.dumps(PLANNING))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    supervisor.run("Une date", interactive=True)
    assert supervisor.event_data["results"]["planning"] == PLANNING
    assert seen == ["Supervisor", "planning agent"]


@pytest.mark.parametrize("refuse_plan", [True, False])
def test_human_refusal_still_stops_before_later_tool_calls(monkeypatch, refuse_plan):
    from model.agents import tools

    saved = event_state.EventRun("Rédige un email")
    saved.event_data["requirements"].update(document_kind="email", budget_total=10000)
    saved.event_data["results"]["budget"] = {"venue": 0, "catering": 0, "provider": 0}
    saved.event_data["tasks"]["budget"] = {"status": "success"}
    ask = Mock(side_effect=[{"approved": False}] if refuse_plan else [{"approved": True}, {"approved": False}])
    monkeypatch.setattr(tools, "ask_user", ask)
    seen = []
    executed = []
    original_execute = agent_module.Agent.execute_tool

    def execute(self, appel):
        executed.append(appel.function.name)
        return original_execute(self, appel)

    def fake(messages, agent=None, **kwargs):
        seen.append(agent)
        if agent == "Supervisor":
            return message(None, call("writer_agent", demande="Email", tasks=["documents"]),
                           call("budgetary_agent", demande="Budget déjà fait", tasks=["budget"]))
        assert agent == "writer agent"
        return message(None, call("ask_user", message="  Objet : brouillon exact.  "))

    monkeypatch.setattr(agent_module.Agent, "execute_tool", execute)
    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    supervisor.run("Rédige un email", event_data=saved.event_data)
    assert "budgetary_agent" not in executed
    assert supervisor.event_data["tasks"]["documents"]["status"] == "refused"
    if refuse_plan:
        assert seen == ["Supervisor"] and ask.call_count == 1
    else:
        assert seen == ["Supervisor", "writer agent"] and ask.call_count == 2
        assert supervisor.event_data["results"]["documents"][0]["content"] == "  Objet : brouillon exact.  "
