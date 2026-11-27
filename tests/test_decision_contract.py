"""Replay minimal de 4d4b9578 et contrat de délégation, sans LLM ni réseau réels."""
import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from model import agent as agent_module, event_state, trace
from model.event_orchestrator import create_orchestrator
from model.event_state import EventRun
from controller.event_controller import run_conversation, continue_conversation
from dao.conversation_dao import load_conversation


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "decision_4d4b9578.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 26)
    monkeypatch.setattr(event_state, "date", Today)
    monkeypatch.setattr(trace, "tracer", Mock())
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("LLM non simulé")))


def bound_run(student=True):
    run = EventRun("Étudiant, pour 100 personnes, maximum 10 000 €, un samedi de préférence." if student else
                   "Un anniversaire pour 100 personnes à Namur", interactive=True)
    run.remember({"city": "Namur", "location_scope": "city", "guests": 100, "budget_total": 10000,
                  **({"event_type": "soiree_etudiante"} if student else {})}, ["venue", "budget", "planning"])
    run.event_data["results"]["budget"] = {"venue": .4, "catering": .3, "provider": .3}
    run.event_data["results"]["planning"] = {"start": "2026-11-28T20:00", "end": "2026-11-29T02:00" if student else "2026-11-28T23:00"}
    if student:
        run.event_data["requirements"]["preferences"] = run.turn_request
    for task in ("budget", "planning"):
        run.event_data["tasks"][task] = {"status": "success"}
    supervisor = create_orchestrator()
    supervisor._bind(run)
    return run, supervisor


def test_decision_schema_requires_explicit_category_and_small_business_requirements():
    _, supervisor = bound_run()
    schema = next(item["function"] for item in supervisor.schemas if item["function"]["name"] == "decision_making_agent")
    parameters = schema["parameters"]
    assert "category" in parameters["required"]
    assert parameters["properties"]["category"]["enum"] == ["venue", "catering", "provider"]
    assert all(value in parameters["properties"]["category"]["description"] for value in ("venue", "catering", "provider"))
    assert set(parameters["properties"]) == {"instruction", "category"}
    assert parameters["required"] == ["instruction", "category"]


@pytest.mark.parametrize("category", [None, "", "salle", ["venue"]])
def test_missing_or_ambiguous_category_never_reaches_business_logic(monkeypatch, category):
    run, supervisor = bound_run()
    delegate = Mock(side_effect=AssertionError("Ne pas atteindre la logique métier"))
    monkeypatch.setattr(run, "delegate", delegate)
    arguments = {"demande": "Salle", **({"category": category} if category is not None else {})}
    result = supervisor.fonctions["decision_making_agent"](**arguments)
    assert result["code"] == "invalid_arguments" and "category" in result["error"]
    delegate.assert_not_called()
    agent_module.chat.assert_not_called()


def test_logged_arguments_do_not_invent_constraints_or_writer_task():
    run, supervisor = bound_run()
    arguments = copy.deepcopy(FIXTURE["calls"][1]["arguments"])
    result = supervisor.fonctions["decision_making_agent"](**arguments)
    assert result["status"] == "no_match"
    req = run.event_data["requirements"]
    assert not set(req) & {"provider_vat_rate", "provider_distance_km", "catering_distance_km", "provider_vehicles",
                           "document_kind", "venue_filters", "catering_filters", "provider_filters", "music_required"}
    assert "documents" not in run.event_data["tasks"]
    filters = run.context("venue")["filters"]
    assert not set(filters) & {"accessible", "seated", "space_type"}
    assert filters["event_type"] == "soiree_etudiante" and filters["max_budget"] == 4000
    assert run.event_data["tasks"]["planning"]["status"] == run.event_data["tasks"]["budget"]["status"] == "success"
    agent_module.chat.assert_not_called()


def test_compatible_candidates_require_a_qualitative_specialist_call(monkeypatch):
    run, supervisor = bound_run(student=False)
    calls = []
    def select(messages, agent=None, **kwargs):
        calls.append(agent)
        payload = json.loads(messages[1]["content"])
        assert not set(payload["filters"]) & {"accessible", "seated", "space_type"}
        assert "catering_filters" not in payload and "provider_filters" not in payload
        return SimpleNamespace(content=json.dumps({"id": payload["search"]["candidates"][-1]["id"]}), tool_calls=None)
    monkeypatch.setattr(agent_module, "chat", select)
    result = supervisor.fonctions["decision_making_agent"](category="venue", demande="Salle",
        requirements={"venue_filters": {"accessible": True, "seated": True, "space_type": "les deux"},
                      "provider_vat_rate": .2, "provider_distance_km": 0, "document_kind": "email"})
    assert result["status"] == "success" and calls == ["decision making agent"]
    assert run.event_data["results"]["venue"]["id"] == result["result"]["id"]


def test_logged_revision_does_not_reintroduce_invented_filters_or_restart_no_match():
    run, supervisor = bound_run()
    supervisor.fonctions["decision_making_agent"](category="venue", demande="Salle")
    before = copy.deepcopy(run.event_data)
    attempts = copy.deepcopy(run.attempts)
    revision = copy.deepcopy(FIXTURE["calls"][2]["arguments"])
    result = supervisor.fonctions["revise_event"](**revision)
    assert result["status"] == "unchanged"
    assert run.event_data == before and run.attempts == attempts


def test_unrequested_writer_and_document_task_are_rejected_without_execution():
    run, supervisor = bound_run()
    result = supervisor.fonctions["writer_agent"](demande="Rédige un email", tasks=["documents"], requirements={"document_kind": "email"})
    assert result["status"] == "not_requested"
    assert "documents" not in run.event_data["tasks"] and "document_kind" not in run.event_data["requirements"]
    agent_module.chat.assert_not_called()


def tool_message(name, **arguments):
    return SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id=name,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))])


def test_real_scenario_two_messages_saved_reopened_and_useful_revision(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=tool_message("clarify", question="Quel type et combien de personnes ?")))
    conversation = run_conversation("Organise un événement uniquement à Namur.", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert conversation["event_data"]["workflow"]["pending_input"]["kind"] == "clarification"
    answer = "Étudiant, pour 100 personnes, maximum 10 000 €, un samedi de préférence."
    stages = iter([
        tool_message("budgetary_agent", demande="Répartition", requirements={"city": "Namur", "guests": 100,
            "event_type": "soiree_etudiante", "budget_total": 10000, "weekday": "saturday"}, tasks=["planning", "budget", "venue"]),
        tool_message("planning_agent", demande="Date un samedi de préférence"),
        tool_message("decision_making_agent", **FIXTURE["calls"][0]["arguments"]),
        tool_message("decision_making_agent", **FIXTURE["calls"][1]["arguments"]),
        tool_message("revise_event", **FIXTURE["calls"][2]["arguments"]),
        SimpleNamespace(content="Aucune salle compatible à Namur ; les décisions indépendantes restent acquises.", tool_calls=None),
    ])
    agents = []
    def simulate(messages, agent=None, **kwargs):
        agents.append(agent)
        if agent == "Supervisor":
            return next(stages)
        if agent == "budgetary agent":
            return SimpleNamespace(content=json.dumps({"venue": 1, "catering": 0, "provider": 0}), tool_calls=None)
        if agent == "planning agent":
            payload = json.loads(messages[1]["content"])
            assert "weekday" not in payload["requirements"]
            assert "préférence" in payload["requirements"]["preferences"]
            return SimpleNamespace(content=json.dumps({"start": "2026-11-28T20:00", "end": "2026-11-29T02:00"}), tool_calls=None)
        pytest.fail("Aucun Decision-Making nécessaire sans candidat compatible")
    monkeypatch.setattr(agent_module, "chat", simulate)
    conversation = continue_conversation(conversation, answer, directory=tmp_path, orchestrator_factory=create_orchestrator)
    data = conversation["event_data"]
    assert data["tasks"]["venue"]["status"] == "no_match"
    assert "documents" not in data["tasks"]
    assert not set(data["requirements"]) & {"provider_vat_rate", "provider_distance_km", "catering_distance_km",
                                            "venue_filters", "catering_filters", "provider_filters", "weekday"}
    assert data["results"]["planning"] and data["results"]["budget"] and conversation["status"] == "partial"
    assert agents.count("planning agent") == agents.count("budgetary agent") == 1
    assert "decision making agent" not in agents
    reopened = load_conversation(conversation["conversation_id"], tmp_path)
    assert reopened == conversation
    original_plan, original_budget = copy.deepcopy(data["results"]["planning"]), copy.deepcopy(data["results"]["budget"])
    run = EventRun("Cherche dans les environs, dans la province de Namur", event_data=reopened["event_data"], interactive=True)
    supervisor = create_orchestrator()
    supervisor._bind(run)
    result = supervisor.fonctions["revise_event"](requirements={"location_scope": "province_fallback"})
    assert result["status"] == "updated" and run.event_data["tasks"]["venue"]["status"] == "invalidated"
    assert run.event_data["results"]["planning"] == original_plan and run.event_data["results"]["budget"] == original_budget
    def choose(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        assert payload["location_search"]["expanded"]
        assert payload["filters"]["province"] == "Namur" and payload["filters"]["city"] == "Namur"
        assert payload["location_search"]["selected_city"] == "Dinant"
        return SimpleNamespace(content=json.dumps({"id": payload["search"]["candidates"][0]["id"]}), tool_calls=None)
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=choose))
    assert supervisor.fonctions["decision_making_agent"](category="venue", demande="Salle dans la province")["status"] == "success"
    assert agent_module.chat.call_count == 1
    assert supervisor.fonctions["decision_making_agent"](category="venue", demande="Même recherche")["status"] == "reused"
    assert agent_module.chat.call_count == 1


def test_confirmed_constraints_are_reused_even_if_not_repeated_in_message():
    run, supervisor = bound_run(student=False)
    run.event_data["requirements"]["venue_constraints"] = {"accessible": True, "seated": True}
    captured = Mock(return_value={"status": "captured"})
    run.delegate = captured
    supervisor.fonctions["decision_making_agent"](category="venue", demande="Salle")
    assert run.context("venue")["filters"]["accessible"] is True
    assert run.context("venue")["filters"]["seated"] is True


def test_explicit_accessibility_and_seating_build_filters_in_python():
    run = EventRun("Salle avec accès PMR obligatoire, places assises, intérieur et extérieur", interactive=True)
    req = run.supervisor_requirements({"accessible": True, "seated": True, "space_type": "les deux"}, category="venue")
    run.remember(req)
    filters = run.context("venue")["filters"]
    assert filters["accessible"] is True and filters["seated"] is True and filters["space_type"] == "les deux"
    assert "venue_filters" not in run.event_data["requirements"]


@pytest.mark.parametrize("rate,distance", [(0, 0), (.2, 25)])
def test_explicit_financial_values_remain_valid_without_assumed_defaults(rate, distance):
    run = EventRun(f"TVA {rate * 100:g} %, prestataire à {distance} km, traiteur à {distance} km, 1 véhicule", interactive=True)
    values = {"provider_vat_rate": rate, "provider_distance_km": distance,
              "catering_distance_km": distance, "provider_vehicles": 1}
    assert run.supervisor_requirements(values) == values


def test_explicit_email_request_still_creates_pending_plan_approval():
    run, supervisor = bound_run(student=False)
    run.event_data["results"]["venue"] = {"id": "salle_010"}
    run.event_data["tasks"]["venue"] = {"status": "success"}
    run.turn_request = "Rédige un email de demande de devis à la salle"
    assert supervisor.fonctions["writer_agent"](demande="Email", requirements={"document_kind": "email"}, tasks=["documents"])["status"] == "waiting"
    assert run.event_data["workflow"]["pending_input"]["kind"] == "plan_approval"
    agent_module.chat.assert_not_called()


def test_identical_no_match_and_replace_do_not_repeat_search(monkeypatch):
    run, supervisor = bound_run()
    from model import event_state as state
    search = Mock(wraps=state.search_candidates)
    monkeypatch.setattr(state, "search_candidates", search)
    assert supervisor.fonctions["decision_making_agent"](category="venue", demande="Salle")["status"] == "no_match"
    calls = search.call_count
    assert supervisor.fonctions["revise_event"](requirements={}, replace=["venue"])["status"] == "unchanged"
    assert supervisor.fonctions["decision_making_agent"](category="venue", demande="Autre formulation identique")["status"] == "reused"
    assert search.call_count == calls
    agent_module.chat.assert_not_called()


def test_stringified_requirements_are_rejected_before_state_mutation(monkeypatch):
    run, supervisor = bound_run()
    delegate = Mock(side_effect=AssertionError("Contrat invalide"))
    monkeypatch.setattr(run, "delegate", delegate)
    result = supervisor.fonctions["decision_making_agent"](category="venue", demande="Salle", requirements='{"city":"Namur"}')
    assert result["code"] == "invalid_arguments"
    delegate.assert_not_called()


def test_weekday_preference_does_not_cancel_an_independent_accessibility_requirement():
    run = EventRun("Accès PMR obligatoire, un samedi de préférence.", interactive=True)
    requirements = run.supervisor_requirements({"accessible": True, "weekday": "saturday"})
    assert requirements["accessible"] is True
    assert "weekday" not in requirements and "samedi" in requirements["preferences"]


@pytest.mark.parametrize("wording", ["Sans email", "Pas d'email", "Pas de mail", "Aucun courriel"])
def test_unwanted_document_cannot_be_added(wording):
    run = EventRun(wording + ". Une salle seulement.", interactive=True)
    assert not run.document_requested()
    assert run.supervisor_requirements({"document_kind": "email"}) == {}


def test_unknown_null_budget_does_not_erase_known_budget_but_explicit_unlimited_does():
    run, supervisor = bound_run()
    assert run.supervisor_requirements({"budget_total": None}) == {}
    run.turn_request = "Finalement, budget illimité"
    result = supervisor.fonctions["revise_event"](requirements={"budget_total": None})
    assert result["status"] == "updated"
    assert "budget_total" not in run.event_data["requirements"] and run.event_data["results"]["budget"] is None


def test_unknown_headcount_and_budget_are_not_manufactured_from_schema_examples():
    run = EventRun("Organise un événement à Namur", interactive=True)
    assert run.supervisor_requirements({"guests": 100, "budget_total": 0}) == {}


def test_explicit_legacy_accessibility_filter_is_translated_to_a_business_constraint():
    run = EventRun("Une salle PMR avec des places assises obligatoires", interactive=True)
    assert run.supervisor_requirements({"venue_filters": {"accessible": True, "seated": True,
                                                          "space_type": "les deux", "limit": 5}}, category="venue") == {
        "accessible": True, "seated": True}
