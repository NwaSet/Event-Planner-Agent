"""Incidents réels 48267a87 / 111ef5d1 : aucun modèle ni réseau réels."""
import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from model import agent as agent_module, event_state, trace
from model.agents import tools, create_all_agents
from model.agents.search import load_catalogs, search_candidates
from model.event_orchestrator import create_orchestrator
from model.event_state import EventRun
from controller.event_controller import run_conversation, continue_conversation
from dao.conversation_dao import load_conversation

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "afterwork_48267a87.json").read_text(encoding="utf-8"))
INITIAL, ANSWER = [item["content"] for item in FIXTURE["messages"][:2]]
PLANNING = {"start": "2026-12-05T19:30", "end": "2026-12-05T23:30"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 27)
    monkeypatch.setattr(event_state, "date", Today)
    monkeypatch.setattr(trace, "tracer", Mock())
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("LLM non simulé")))


def test_logged_revision_resolves_date_and_budget_without_llm_repair():
    previous = EventRun(INITIAL, interactive=True).event_data
    run = EventRun(ANSWER, event_data=previous, interactive=True)
    supervisor = create_orchestrator()
    supervisor._bind(run)
    result = supervisor.fonctions["revise_event"](**FIXTURE["revisions"][0]["arguments"])
    assert result["status"] == "updated"
    req = run.event_data["requirements"]
    assert req["date"] == "2026-12-05"
    assert req["event_type"] == "reception_entreprise"
    assert run.event_data["tasks"].get("budget", {}).get("status") != "pending"
    assert run.conversation_context([])["today"] == "2026-11-27"


def test_afterwork_alias_and_meal_filter_reach_real_candidates():
    catalogs = load_catalogs()
    afterwork = search_candidates("venue", {"event_type": "afterwork", "guests": 100, "city": "Namur"}, catalogs=catalogs)
    assert afterwork["status"] == "matched"
    assert afterwork["filters"]["event_type"] == "reception_entreprise"
    assert all("reception_entreprise" in row["types_evenements_adaptes"] for row in afterwork["candidates"])
    run = EventRun(ANSWER, interactive=True)
    cleaned = run.supervisor_requirements({"catering_constraints": {"event_type": "afterwork", "full_meal": True,
                                                                    "service_type": "cocktail", "guests": 100}})
    assert "service_type" not in cleaned["catering_constraints"]
    assert "full_meal" not in cleaned["catering_constraints"]  # « préfère un bon repas » ne fixe pas de formule.
    meal = search_candidates("catering", cleaned["catering_constraints"], catalogs=catalogs,
                             context={"date": "2026-12-05", "today": "2026-11-27"})
    assert meal["status"] == "matched"


def test_budget_announcement_without_ceiling_is_not_pending():
    run = EventRun("Pas de limite de budget", interactive=True)
    run.remember(tasks=["planning", "budget", "venue"])
    assert run.event_data["tasks"].get("budget", {}).get("status") != "pending"


def test_explicit_past_date_is_rejected():
    run = EventRun("Le 6 décembre 2025", interactive=True)
    with pytest.raises((ValueError, event_state.TaskImpossible), match="passée|passé"):
        run.remember({"date": "2025-12-06"})


def message(content=None, *calls):
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


def call(name, **arguments):
    return SimpleNamespace(id=name, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def choose(messages):
    payload = json.loads(messages[1]["content"])
    candidate = payload["search"]["candidates"][0]
    selected = {"id": candidate["id"]}
    if payload["task"] == "catering":
        selected["formula_id"] = candidate["formules"][0]["id"]
    elif payload["task"] == "provider":
        selected["kit_id"] = candidate["kits"][0]["id"]
    return message(json.dumps(selected))


@pytest.mark.parametrize("with_provider", [False, True])
def test_complete_clarification_selections_and_writer_within_existing_limits(monkeypatch, tmp_path, with_provider):
    supervisor = create_orchestrator()
    observed = []
    stages = iter(["clarify", "revise", "delegate"])
    draft = "Objet : Devis afterwork du 5 décembre 2026\nMerci de confirmer vos disponibilités."

    def simulate(messages, agent=None, **kwargs):
        observed.append(agent)
        payload = json.loads(messages[1]["content"])
        if agent == "Supervisor":
            assert payload["today"] == "2026-11-27"
            stage = next(stages)
            if stage == "clarify":
                return message(None, call("clarify", question="Quel type, quel effectif et quelle date ?"))
            if stage == "revise":
                args = copy.deepcopy(FIXTURE["revisions"][0]["arguments"])
                if not with_provider:
                    args["tasks"].remove("provider")
                return message(None, call("revise_event", **args))
            assert json.loads(messages[-1]["content"])["requirements"]["date"] == "2026-12-05"
            return message(None, call("planning_agent", instruction="Horaires"),
                           call("decision_making_agent", category="venue", instruction="Salle"),
                           call("decision_making_agent", category="catering", instruction="Traiteur"),
                           *([call("decision_making_agent", category="provider", instruction="Technique")] if with_provider else []),
                           call("writer_agent", instruction="Email au traiteur"))
        if agent == "planning agent":
            assert payload["requirements"]["date"] == "2026-12-05"
            return message(json.dumps(PLANNING))
        if agent == "decision making agent":
            assert payload["search"]["candidates"]
            if payload["task"] == "catering":
                assert "service_type" not in payload["filters"]
            return choose(messages)
        assert agent == "writer agent" and payload["plan_approved"] is True
        assert payload["document"]["recipient"] == "catering"
        assert payload["catering"]["offer"]["id"]
        return message(None, call("ask_user", message=draft))

    monkeypatch.setattr(agent_module, "chat", simulate)
    factory = lambda: supervisor
    conversation = run_conversation(INITIAL, directory=tmp_path, orchestrator_factory=factory)
    assert conversation["status"] == "waiting"
    conversation = continue_conversation(conversation, ANSWER, directory=tmp_path, orchestrator_factory=factory)
    data = conversation["event_data"]
    assert data["workflow"]["pending_input"], (observed, data["tasks"], conversation["messages"][-1])
    assert data["workflow"]["pending_input"]["kind"] == "plan_approval"
    assert data["tasks"]["budget"]["status"] == "skipped"
    assert data["results"]["venue"] and data["results"]["catering"]
    assert bool(data["results"]["providers"]) == with_provider
    assert "budgetary agent" not in observed and "writer agent" not in observed
    assert supervisor.max_iterations == 6 and supervisor.counters["llm_calls"] <= 6
    selections = copy.deepcopy(data["results"])
    conversation = continue_conversation(load_conversation(conversation["conversation_id"], tmp_path), "oui",
                                         directory=tmp_path, orchestrator_factory=factory)
    assert conversation["event_data"]["workflow"]["pending_input"]["kind"] == "email_approval"
    count = len(observed)
    conversation = continue_conversation(conversation, "oui", directory=tmp_path, orchestrator_factory=factory)
    assert len(observed) == count and conversation["status"] == "completed"
    assert conversation["event_data"]["results"]["documents"][0]["content"] == draft
    for category in ("planning", "venue", "catering", "providers"):
        assert conversation["event_data"]["results"][category] == selections[category]


@pytest.mark.parametrize("scope", ["nearest", "province_fallback", "city"])
def test_existing_geography_runs_only_after_business_filtering(monkeypatch, scope):
    run = EventRun("Afterwork à Namur", interactive=True)
    ids = [row["id"] for row in run.catalogs["venue"] if row["lieux"]["commune"] in ("Dinant", "Arlon")
           and "reception_entreprise" in row["types_evenements_adaptes"]]
    run.remember({"city": "Namur", "location_scope": scope, "event_type": "afterwork", "venue_constraints": {"ids": ids}}, ["venue"])
    rank = Mock(wraps=tools.select_filtered)
    monkeypatch.setattr(tools, "select_filtered", rank)
    context = run.context("venue")
    result = run.search_options("venue", context["filters"], context)
    if scope == "city":
        assert result["status"] == "no_match"
        rank.assert_not_called()
    else:
        assert result["status"] == "matched"
        assert all(row["lieux"]["commune"] == "Dinant" for row in result["candidates"])
        rank.assert_called_once()
        assert rank.call_args.args[1] == "Namur"


def test_no_venue_keeps_independent_services_and_explicit_partial_result(monkeypatch):
    run = EventRun("Afterwork à Namur. Génère un email au traiteur.", interactive=True)
    run.remember({"city": "Namur", "guests": 100, "event_type": "afterwork", "planning": PLANNING,
                  "venue_constraints": {"max_budget": 1}}, ["venue", "catering", "provider", "documents", "budget"])
    _, writer, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", lambda messages, **kwargs: choose(messages))
    assert run.delegate(writer, "Email", "documents")["requires"] == "catering"
    assert run.delegate(decision, "Salle", "venue")["status"] == "no_match"
    assert run.delegate(decision, "Traiteur provisoire", "catering")["status"] == "success"
    assert run.delegate(decision, "Technique provisoire", "provider")["status"] == "success"
    assert run.delegate(writer, "Email", "documents")["status"] == "blocked"
    assert run.complete() and not run.successful() and not run.can_repair()
    assert "future salle à confirmer" in run.summary("")
    assert "aucune salle sélectionnée" in run.summary("")


def test_explicit_cocktail_full_meal_is_not_silently_relaxed():
    run = EventRun("Je veux un cocktail et un repas complet", interactive=True)
    requested = {"catering_constraints": {"service_type": "cocktail", "full_meal": True}}
    assert run.supervisor_requirements(requested) == requested
    result = search_candidates("catering", requested["catering_constraints"])
    assert result["status"] == "no_match"
    assert search_candidates("venue", {"event_type": "type_inconnu"})["status"] == "no_match"


def test_saved_real_failure_reopens_without_reinventing_constraints():
    original = copy.deepcopy(FIXTURE["event_data"])
    run = EventRun("organise la soirée", event_data=original, messages=FIXTURE["messages"], interactive=True)
    req = run.event_data["requirements"]
    assert req["event_type"] == "reception_entreprise" and req["date"] == "2026-12-05"
    assert "service_type" not in req.get("catering_constraints", {})
    assert "full_meal" not in req.get("catering_constraints", {})
    assert run.event_data["tasks"]["budget"]["status"] == "skipped"
    assert run.event_data["tasks"]["venue"]["status"] in ("invalidated", "pending")
    assert run.event_data["tasks"]["documents"]["document"]["recipient"] == "catering"
    assert original == FIXTURE["event_data"]
    context = run.context("venue")
    result = run.search_options("venue", context["filters"], context, run.services_available)
    assert result["status"] == "matched"


@pytest.mark.parametrize("text,today,expected", [
    ("premier samedi de décembre", "2026-11-27", "2026-12-05"),
    ("premier samedi de décembre", "2026-12-06", "2027-12-04"),
    ("premier samedi de décembre 2025", "2026-11-27", "2025-12-06"),
])
def test_first_weekday_uses_current_year_or_explicit_year(text, today, expected):
    assert event_state.first_weekday_date(text, today) == expected


def test_alias_preserves_catalogue_prohibitions():
    row = copy.deepcopy(load_catalogs()["venue"][0])
    row["types_evenements_adaptes"] = ["reception_entreprise"]
    row["types_evenements_interdits"] = ["reception_entreprise"]
    assert not tools._venue_match(row, {"event_type": "afterwork"})


def test_repeated_verbose_revision_is_unchanged_and_does_not_restart_tasks():
    run = EventRun(ANSWER, interactive=True)
    supervisor = create_orchestrator()
    supervisor._bind(run)
    arguments = FIXTURE["revisions"][2]["arguments"]
    supervisor.fonctions["revise_event"](**arguments)
    run.record("planning", {"status": "success", "result": PLANNING})
    revision = run.event_data["workflow"]["revision"]
    assert supervisor.fonctions["revise_event"](**arguments)["status"] == "unchanged"
    assert run.event_data["workflow"]["revision"] == revision
    assert run.event_data["results"]["planning"] == PLANNING
    assert not run.event_data["requirements"].get("catering_constraints")


def test_new_explicit_date_overrides_earlier_relative_request():
    run = EventRun("Change la date au 12 décembre 2026", messages=[{"role": "user", "content": ANSWER}], interactive=True)
    assert run.supervisor_requirements({"date": "2026-12-12"})["date"] == "2026-12-12"


def test_independent_services_keep_known_province_without_a_city():
    run = EventRun("Dans la province de Namur", interactive=True)
    run.remember({"province": "Namur"}, ["venue", "catering", "provider"])
    run.record("venue", {"status": "no_match", "error": "Aucune salle compatible."})
    assert run.context("catering")["filters"]["province"] == "Namur"
    assert run.context("provider")["filters"]["province"] == "Namur"
