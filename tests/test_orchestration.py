"""Scénarios métier sans réseau ni facturation LLM."""

import copy
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from model import agent as agent_module, event_state, llm, trace
from model.agent import Agent
from model.agents import create_all_agents, tools
from model.agents.search import compact_tool, load_catalogs, search_candidates
from model.event_orchestrator import create_orchestrator
from model.event_state import EventRun, encode


PLANNING = {"start": "2026-12-19T20:00", "end": "2026-12-20T00:00"}


def message(content=None, *calls):
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


def call(name, **arguments):
    return SimpleNamespace(id=name + str(len(encode(arguments))),
                           function=SimpleNamespace(name=name, arguments=encode(arguments)))


@pytest.fixture(autouse=True)
def quiet_and_fixed_date(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 24)

    monkeypatch.setattr(event_state, "date", Today)
    monkeypatch.setattr(trace, "tracer", Mock())
    # Toute tentative LLM doit être remplacée explicitement par le scénario.
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("LLM non simulé")))


def select_first(messages, **kwargs):
    payload = json.loads(messages[1]["content"])
    candidates = payload["search"]["candidates"]
    if not candidates:
        if len(messages) == 2:
            return message(None, call({"venue": "get_venues", "catering": "get_caterers", "provider": "get_providers"}[payload["task"]]))
        candidates = json.loads(messages[-1]["content"])["candidates"]
    candidate = candidates[0]
    selected = {"id": candidate["id"]}
    if payload["task"] == "catering":
        selected["formula_id"] = candidate["formules"][0]["id"]
    if payload["task"] == "provider":
        selected["kit_id"] = candidate["kits"][0]["id"]
    return message(encode(selected))


def prepared(requirements=None, tasks=None):
    run = EventRun("Demande de test")
    run.remember(requirements or {}, tasks or ["venue"])
    return run


def test_a_direct_planning_without_requirements_or_task_list(monkeypatch):
    request = "Propose une date pour une soirée à Namur."
    rounds = []

    def fake(messages, tools=None, agent=None):
        if agent == "planning agent":
            payload = json.loads(messages[1]["content"])
            assert payload["request"] == request and payload["requirements"] == {}
            return message(encode(PLANNING))
        rounds.append(1)
        if len(rounds) == 1:
            assert {schema["function"]["name"] for schema in tools} == {
                "planning_agent", "budgetary_agent", "decision_making_agent", "writer_agent"}
            return message(None, call("planning_agent", demande="Proposer une date et des horaires"))
        return message("Date proposée : 19 décembre 2026, de 20 h à minuit.")

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    assert "19 décembre" in supervisor.run(request)
    assert supervisor.last_status == "success"
    assert supervisor.event_data["planning"] == PLANNING
    assert supervisor.counters["llm_calls"] == 3


def test_b_direct_venue_specialist_searches_namur_without_configuration(monkeypatch):
    rounds = []

    def fake(messages, tools=None, agent=None):
        if agent == "decision making agent":
            assert [schema["function"]["name"] for schema in tools] == ["get_venues"]
            if len(messages) == 2:
                assert json.loads(messages[1]["content"])["search"]["candidates"] == []
                return message(None, call("get_venues", city="Namur"))
            return select_first(messages)
        rounds.append(1)
        if len(rounds) == 1:
            return message(None, call("decision_making_agent", category="venue", demande="Salle à Namur"))
        return message("Salle proposée à Namur, disponibilité à confirmer.")

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    supervisor.run("Trouve une salle à Namur.")
    assert supervisor.last_status == "success"
    row = next(row for row in load_catalogs()["venue"] if row["id"] == supervisor.event_data["venue"]["id"])
    assert row["lieux"]["commune"] == "Namur"
    assert supervisor.event_data["planning"] is None
    assert supervisor.counters["llm_calls"] == 4


def test_c_multiple_delegations_without_a_declared_plan(monkeypatch):
    rounds = []

    def fake(messages, tools=None, agent=None):
        if agent == "decision making agent":
            return select_first(messages)
        rounds.append(1)
        if len(rounds) == 1:
            return message(None, call("decision_making_agent", category="venue", demande="Salle",
                                      requirements={"city": "Namur"}))
        if len(rounds) == 2:
            return message(None, call("decision_making_agent", category="catering", demande="Traiteur compatible"))
        return message("Salle et traiteur proposés, disponibilité à confirmer.")

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    supervisor.run("Organise une soirée à Namur avec une salle et un traiteur.")
    data = supervisor.event_data
    assert supervisor.last_status == "success"
    assert data["venue"] and data["catering"] and data["planning"] is None
    venue = next(row for row in load_catalogs()["venue"] if row["id"] == data["venue"]["id"])
    caterer = next(row for row in load_catalogs()["catering"] if row["id"] == data["catering"]["id"])
    assert venue["lieux"]["commune"] == "Namur"
    assert event_state.compatible("catering", caterer, {"venue": venue})
    assert supervisor.counters["llm_calls"] == 5


@pytest.mark.parametrize("failure", ["content_filter", "json"])
def test_e_planning_failure_keeps_all_three_catalogue_decisions(monkeypatch, failure):
    rounds, planning_calls = [], []

    def fake(messages, tools=None, agent=None):
        if agent == "planning agent":
            planning_calls.append(1)
            if failure == "content_filter":
                raise llm.ErreurLLM("Réponse du modèle interrompue : content_filter")
            return message("JSON invalide")
        if agent == "decision making agent":
            return select_first(messages)
        if tools is None:
            return message("Proposition partielle, sans date.")
        rounds.append(1)
        if len(rounds) == 1:
            return message(None, call("planning_agent", demande="Date", requirements={"city": "Namur"},
                                      tasks=["planning", "venue", "catering", "provider"]))
        if len(rounds) == 2:
            return message(None, call("decision_making_agent", category="venue", demande="Salle sans date confirmée"))
        return message(None, call("decision_making_agent", category="catering", demande="Traiteur"),
                       call("decision_making_agent", category="provider", demande="Prestataire"))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    output = supervisor.run("Organise une soirée à Namur sans limite de budget : date, salle, traiteur et prestataire.")
    data = supervisor.event_data
    assert supervisor.last_status == "partial" and data["planning"] is None
    assert data["venue"] and data["catering"] and data["providers"]
    assert all(data["tasks"][task]["status"] == "success" for task in ("venue", "catering", "provider"))
    assert data["tasks"]["planning"]["code"] == ("llm_error" if failure == "content_filter" else "invalid_output")
    assert len(planning_calls) == (1 if failure == "content_filter" else 2)
    assert all(label in output for label in ("Salle :", "Traiteur :", "Prestataire :", "Non résolu", "Disponibilités non vérifiées"))


def test_direct_delegation_schema_has_only_essential_required_arguments():
    for schema in create_orchestrator().schemas:
        function = schema["function"]
        assert function["parameters"]["required"] == (["demande", "category"]
            if function["name"] == "decision_making_agent" else ["demande"])


def test_unlimited_budget_skips_specialist_without_llm(monkeypatch):
    run = EventRun("Sans limite de budget")
    budget, _, _, _ = create_all_agents()
    assert run.delegate(budget, "Budget", "budget")["status"] == "skipped"
    agent_module.chat.assert_not_called()
    assert run.event_data["budget"] is None and not run.successful()


def test_late_planning_respects_existing_venue_and_repairs_once(monkeypatch):
    run = prepared({"venue_filters": {"ids": ["salle_001"]}}, ["venue", "planning"])
    _, _, planning, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", select_first)
    assert run.delegate(decision, "Salle d'abord", "venue")["status"] == "success"
    chat = Mock(side_effect=[message(encode({"start": "2026-12-19T20:00", "end": "2026-12-20T03:00"})),
                             message(encode(PLANNING))])
    monkeypatch.setattr(agent_module, "chat", chat)
    assert run.delegate(planning, "Horaires compatibles", "planning")["status"] == "success"
    assert chat.call_count == 2
    assert json.loads(chat.call_args.args[0][1]["content"])["venue_constraints"]["heure_fin_musique"] == "02:00"
    assert run.event_data["planning"] == PLANNING


def test_late_venue_preserves_previously_selected_provider(monkeypatch):
    run = prepared({"provider_filters": {"ids": ["005"]}}, ["provider", "venue"])
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", select_first)
    assert run.delegate(decision, "Prestataire d'abord", "provider")["status"] == "success"
    first = copy.deepcopy(run.catalogs["venue"][0])
    second = copy.deepcopy(first)
    second["id"] = "compatible"
    first["contraintes"]["prestataires_techniques_autorises_ids"] = ["001"]
    second["contraintes"]["prestataires_techniques_autorises_ids"] = ["005"]
    run.catalogs["venue"] = [first, second]
    assert run.delegate(decision, "Salle compatible", "venue")["status"] == "success"
    assert run.event_data["venue"]["id"] == "compatible"


def test_late_budget_cannot_validate_a_previously_overpriced_selection(monkeypatch):
    run = prepared({"venue_filters": {"ids": ["salle_001"]}}, ["venue"])
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", select_first)
    assert run.delegate(decision, "Salle", "venue")["status"] == "success"
    run.remember({"budget_total": 1})
    with pytest.raises(ValueError, match="incompatible"):
        run.validate_budget('{"venue":1,"catering":0,"provider":0}')
    assert run.event_data["budget"] is None


def test_exact_date_is_kept_and_catering_lead_time_checked_without_schedule():
    run = prepared({"date": "2026-11-25", "catering_filters": {"ids": ["traiteur_001"]}}, ["catering"])
    context = run.context("catering")
    result = search_candidates("catering", context["filters"], catalogs=run.catalogs, context=context)
    assert result["status"] == "no_match"


@pytest.mark.parametrize("field,value", [
    ("province", 12), ("date", "samedi"), ("preferences", []), ("assumptions", {}),
    ("guests", 0), ("budget_total", -1), ("budget_total", ""), ("music_required", "false"),
    ("venue_filters", []), ("venue_filters", {"guests": "cent"}),
    ("venue_filters", {"unknown": None}), ("unknown", None),
])
def test_invalid_requirements_are_local_to_the_task(monkeypatch, field, value):
    run = EventRun("Demande de test")
    _, _, planning, decision = create_all_agents()
    result = run.delegate(decision, "Salle", "venue", requirements={field: value})
    assert result["status"] == "error" and result["code"] == "invalid_requirements"
    assert run.event_data["requirements"] == {}
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message(encode(PLANNING))))
    assert run.delegate(planning, "Date", "planning")["status"] == "success"
    assert not run.successful() and run.event_data["planning"] == PLANNING


def test_missing_catering_headcount_does_not_block_venue(monkeypatch):
    run = prepared({"city": "Namur", "budget_total": 10000}, ["venue", "catering"])
    budget, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message('{"venue":0.5,"catering":0.5,"provider":0}')))
    assert run.delegate(budget, "Répartir", "budget")["status"] == "success"
    monkeypatch.setattr(agent_module, "chat", select_first)
    assert run.delegate(decision, "Salle", "venue")["status"] == "success"
    assert run.delegate(decision, "Traiteur", "catering")["status"] == "invalid_filters"
    assert run.event_data["venue"] and not run.successful()


def test_provider_refusal_is_not_repaired_as_json(monkeypatch):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=None, refusal="Refus explicite", tool_calls=None))])
    monkeypatch.setattr(llm, "client", lambda: client)
    monkeypatch.setattr(llm, "_enregistrer_usage", Mock())
    monkeypatch.setattr(agent_module, "chat", llm.chat)
    run = EventRun("Date")
    _, _, planning, _ = create_all_agents()
    outcome = run.delegate(planning, "Date", "planning")
    assert outcome["code"] == "llm_error" and "Refus" in outcome["error"]
    assert client.chat.completions.create.call_count == 1


def test_reference_namur_end_to_end_and_fresh_second_run(monkeypatch):
    phases, requests = [], []

    def fake_chat(messages, tools=None, agent=None):
        requests.append((agent, copy.deepcopy(messages), tools))
        if agent == "planning agent":
            return message(encode(PLANNING))
        if agent == "decision making agent":
            return select_first(messages)
        if tools is None:
            assert "Disponibilités non vérifiées" in messages[-1]["content"]
            return message("Soirée proposée à Namur, disponibilité à confirmer.")
        phase = len(phases) % 3
        phases.append(phase)
        if phase == 0:
            return message(None, call("planning_agent", demande="Proposer une date et des horaires.",
                                      requirements={"city": "Namur"}, tasks=["planning", "venue", "catering", "provider"]))
        if phase == 1:
            return message(None, call("decision_making_agent", category="venue", demande="Choisir une salle."))
        return message(None, call("decision_making_agent", category="catering", demande="Choisir un traiteur compatible."),
                       call("decision_making_agent", category="provider", demande="Choisir un prestataire compatible."))

    monkeypatch.setattr(agent_module, "chat", fake_chat)
    supervisor = create_orchestrator()
    for _ in range(2):
        assert supervisor.run("Organise une soirée à Namur sans limite de budget. Donne-moi une date, une salle, un traiteur et un prestataire.") == "Soirée proposée à Namur, disponibilité à confirmer."
        data = supervisor.event_data
        assert supervisor.last_status == "success"
        assert data["planning"] == PLANNING and data["venue"] and data["catering"] and data["providers"]
        assert data["budget"] is None and not data["documents"]
        assert supervisor.counters["delegations"] == 4
        assert supervisor.counters["llm_calls"] == 8
        assert all(t["status"] == "success" for t in data["tasks"].values())
        if _ == 0:
            previous = data
        else:
            assert previous is not data and previous["tasks"] is not data["tasks"]
    assert len(requests) == 16
    assert all(agent not in ("budgetary agent", "writer agent") for agent, _, _ in requests)


def test_limited_budget_allocation_and_selected_offer(monkeypatch):
    run = prepared({"budget_total": 5000, "guests": 40, "city": "Namur",
                      "venue_filters": {"rental_period": "soiree"}}, ["venue", "catering", "provider"])
    budget, _, _, decision = create_all_agents()
    assert run.delegate(decision, "Salle", "venue")["status"] == "not_ready"
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message('{"venue":0.4,"catering":0.3,"provider":0.3}')))
    assert run.delegate(budget, "Répartir", "budget")["status"] == "success"
    assert run.context("venue")["filters"]["max_budget"] == 2000
    assert run.context("catering")["filters"]["max_budget"] == 1500
    monkeypatch.setattr(agent_module, "chat", select_first)
    assert run.delegate(decision, "Salle", "venue")["status"] == "success"
    assert run.costs()["known"]["venue_ttc"] <= 2000
    assert run.delegate(decision, "Traiteur", "catering")["status"] == "success"
    assert run.delegate(decision, "Prestataire", "provider")["status"] == "success"
    assert run.costs()["known"]["catering_ttc"] <= 1500
    assert run.costs()["known"]["provider_ht"] <= 1500
    assert run.costs()["allocations"] == {"venue": 2000, "catering": 1500, "provider": 1500}
    assert "non confirmé" in run.costs()["warning"]


@pytest.mark.parametrize("budget", [
    {}, {"venue": .41, "catering": .29, "provider": .3},
    {"venue": .4, "catering": .3, "provider": .4},
    {"venue": True, "catering": 0, "provider": 0},
    {"venue": float("nan"), "catering": 0, "provider": 0},
    {"venue": -0.1, "catering": .8, "provider": .3},
])
def test_invalid_budget_contract(budget):
    run = prepared({}, ["venue", "catering", "provider"])
    with pytest.raises(ValueError):
        run.validate_budget(encode(budget))


def test_budget_absent_categories_and_no_invented_ceiling():
    run = prepared({"budget_total": None}, ["venue"])
    run.validate_budget('{"venue":1,"catering":0,"provider":0}')
    with pytest.raises(ValueError):
        run.validate_budget('{"venue":0.4,"catering":0.3,"provider":0.3}')
    assert "max_budget" not in run.context("venue")["filters"]


@pytest.mark.parametrize("venue_id,category,allowed", [
    ("salle_010", "provider", "prestataires_techniques_autorises_ids"),
    ("salle_001", "catering", "traiteurs_autorises_ids"),
])
def test_mandatory_supplier_ids_survive_delegation(monkeypatch, venue_id, category, allowed):
    run = prepared({"planning": PLANNING, "venue_filters": {"ids": [venue_id]}}, ["venue", category])
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", select_first)
    assert run.delegate(decision, "Salle imposée", "venue")["status"] == "success"
    captured = []

    def choose(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        captured.append(payload)
        ids = run.selected_row("venue")["contraintes"][allowed]
        assert all(candidate["id"] in ids for candidate in payload["search"]["candidates"])
        assert payload["venue"]["contraintes"][allowed] == ids
        return select_first(messages)

    monkeypatch.setattr(agent_module, "chat", choose)
    assert run.delegate(decision, "Choisir", category)["status"] == "success"
    assert captured
    assert run.selected_row(category)["id"] in run.selected_row("venue")["contraintes"][allowed]


def test_simultaneous_constraints_and_no_relaxation(monkeypatch):
    run = prepared({"city": "Namur", "guests": 100, "planning": PLANNING,
                      "venue_filters": {"accessible": True, "space_type": "interieur", "seated": True,
                                        "max_budget": 5000, "rental_period": "soiree"}}, ["venue"])
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", select_first)
    result = run.delegate(decision, "Salle", "venue")
    assert result["status"] == "success"
    venue = run.selected_row("venue")
    assert venue["lieux"]["commune"] == "Namur"
    assert venue["equipements"]["acces_pmr"]
    assert venue["capacite"]["interieur"]["personnes_assises"] >= 100
    assert run.costs()["known"]["venue_ttc"] <= 5000
    with pytest.raises(ValueError, match="Contrainte"):
        run.remember({"city": "Liège"}, ["venue"])


def test_no_match_is_terminal_and_never_exposes_fallback(monkeypatch):
    run = prepared({"city": "Ville impossible"})
    _, _, _, decision = create_all_agents()
    chat = Mock()
    monkeypatch.setattr(agent_module, "chat", chat)
    assert run.delegate(decision, "Choisir", "venue")["status"] == "no_match"
    assert run.delegate(decision, "Choisir encore avec d'autres mots", "venue")["status"] == "reused"
    chat.assert_not_called()
    assert run.complete() and not run.successful()
    assert "Aucune salle" in run.summary("Conflit")
    assert len(tools.get_venues(city="Ville impossible", limit=1)) == 150


@pytest.mark.parametrize("category,filters", [
    ("venue", {"city": "Namur", "max_budget": 1}),
    ("venue", {"event_type": "soiree"}),
    ("provider", {"ids": []}),
    ("catering", {"max_budget": 2000}),
    ("venue", {"guests": "beaucoup"}),
])
def test_compact_search_diagnostics(category, filters):
    result = search_candidates(category, filters)
    assert result["status"] in ("no_match", "invalid_filters")
    assert not result["candidates"] and len(encode(result)) < 300


@pytest.mark.parametrize("category,filters,field", [
    ("catering", {"ids": ["traiteur_001"], "guests": 100, "max_price_per_person": 40}, "formules"),
    ("provider", {"ids": ["005"], "max_budget": 5000}, "kits"),
])
def test_only_compatible_offers_are_projected(category, filters, field):
    result = search_candidates(category, filters)
    assert result["status"] == "matched"
    assert len(result["candidates"][0][field]) < len(load_catalogs()[category][int(filters["ids"][0].split('_')[-1]) - 1][field])
    for row in result["candidates"]:
        for offer in row[field]:
            assert offer["id"] is not None
            if category == "catering":
                assert offer["prix_par_personne_ttc"] <= 40
            else:
                assert offer["prix_estime_eur_ht"] <= 5000


def test_invalid_json_repaired_once_in_same_conversation(monkeypatch):
    run = prepared({}, ["planning"])
    _, _, planning, _ = create_all_agents()
    chat = Mock(side_effect=[message("pas du JSON"), message(encode(PLANNING))])
    monkeypatch.setattr(agent_module, "chat", chat)
    assert run.delegate(planning, "Date", "planning")["status"] == "success"
    assert chat.call_count == 2
    assert "Sortie invalide" in chat.call_args.args[0][-1]["content"]


def test_invalid_output_failure_cached_without_third_attempt(monkeypatch):
    run = prepared({}, ["planning"])
    _, _, planning, _ = create_all_agents()
    chat = Mock(return_value=message("{}"))
    monkeypatch.setattr(agent_module, "chat", chat)
    assert run.delegate(planning, "Date", "planning")["status"] == "error"
    assert run.delegate(planning, "Date encore", "planning")["status"] == "reused"
    assert chat.call_count == 2 and run.event_data["planning"] is None


@pytest.mark.parametrize("selected", [{"id": 5, "kit_id": 1}, {"id": "005", "kit_id": True},
                                      {"id": "005", "kit_id": "1"}, {"id": "001", "kit_id": 1}])
def test_selection_ids_exact_types_and_candidate_membership(monkeypatch, selected):
    run = prepared({"provider_filters": {"ids": ["005"]}}, ["provider"])
    _, _, _, decision = create_all_agents()
    chat = Mock(return_value=message(encode(selected)))
    monkeypatch.setattr(agent_module, "chat", chat)
    assert run.delegate(decision, "Choisir", "provider")["status"] == "error"
    assert run.event_data["providers"] == [] and chat.call_count == 2


def test_duplicate_delegation_reuses_result_despite_paraphrase(monkeypatch):
    run = prepared({"city": "Namur"})
    _, _, _, decision = create_all_agents()
    chat = Mock(side_effect=select_first)
    monkeypatch.setattr(agent_module, "chat", chat)
    assert run.delegate(decision, "Choisir salle", "venue")["status"] == "success"
    assert run.delegate(decision, "Trouver une salle", "venue")["status"] == "reused"
    assert chat.call_count == 1 and run.counters["delegations"] == 1


def test_last_iteration_tools_are_available_for_final_response(monkeypatch):
    observed = []

    def fake(messages, tools=None, **kwargs):
        if tools:
            return message(None, call("compute"))
        observed.extend(copy.deepcopy(messages))
        return message("Résultat : 42")

    monkeypatch.setattr(agent_module, "chat", fake)
    agent = Agent("test", "test", [(lambda: {"value": 42}, llm.tool_schema("compute", "Calcul", {}))], max_iterations=1)
    assert agent.run("Calculer") == "Résultat : 42"
    assert any(item["role"] == "tool" and "42" in item["content"] for item in observed)


def test_global_limit_returns_last_tool_observations(monkeypatch):
    counters = {"llm_calls": 0, "max_llm_calls": 1}
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message(None, call("compute"))))
    agent = Agent("test", "test", [(lambda: {"value": 42}, llm.tool_schema("compute", "Calcul", {}))], max_iterations=1)
    result = json.loads(agent.run("Calculer", counters=counters))
    assert result["status"] == "partial" and "42" in result["observations"][0]["result"]
    assert counters["llm_calls"] == 1


def test_global_budget_shared_by_supervisor_and_nested_agents(monkeypatch):
    def fake(messages, tools=None, agent=None):
        if agent == "Supervisor":
            return message(None, call("planning_agent", demande="Date", tasks=["planning", "venue"]),
                           call("decision_making_agent", category="venue", demande="Salle"))
        return message(encode(PLANNING))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator(max_llm_calls=2)
    output = supervisor.run("Date et salle")
    assert "Résultat partiel" in output and "2026-12-19" in output
    assert "Limite globale" in output
    assert supervisor.event_data["planning"] == PLANNING
    assert supervisor.event_data["venue"] is None
    assert supervisor.counters["llm_calls"] == 2


def test_delegation_limit_preserves_previous_success(monkeypatch):
    run = EventRun("test", max_delegations=1)
    run.remember({}, ["planning", "venue"])
    _, _, planning, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message(encode(PLANNING))))
    assert run.delegate(planning, "Date", "planning")["status"] == "success"
    assert run.delegate(decision, "Salle", "venue")["status"] == "limit"
    assert "2026-12-19" in run.summary("Limite") and run.counters["delegations"] == 1


def test_email_refused_never_finalized_or_reapproved(monkeypatch):
    run = prepared({"document_kind": "email"}, ["documents"])
    _, writer, _, _ = create_all_agents()
    approval = Mock(return_value={"approved": False})
    monkeypatch.setattr(tools, "ask_user", approval)
    chat = Mock(side_effect=[message(None, call("ask_user", message="Objet : demande de disponibilité")),
                             message("Email approuvé !")])
    monkeypatch.setattr(agent_module, "chat", chat)
    assert run.delegate(writer, "Rédiger", "documents")["status"] == "refused"
    assert run.event_data["documents"][0]["approved"] is False
    assert run.delegate(writer, "Rédiger encore", "documents")["status"] == "reused"
    approval.assert_called_once()
    assert not run.successful()


def test_email_requires_approval_of_exact_returned_text(monkeypatch):
    run = prepared({"document_kind": "email"}, ["documents"])
    _, writer, _, _ = create_all_agents()
    monkeypatch.setattr(tools, "ask_user", Mock(return_value={"approved": True}))
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=[message(None, call("ask_user", message="Brouillon validé")),
                                                              message("Texte changé après validation")]))
    assert run.delegate(writer, "Rédiger", "documents")["status"] == "error"
    assert not run.event_data["documents"]


def test_planning_fixed_by_user_skips_specialist(monkeypatch):
    run = prepared({"planning": PLANNING}, ["planning", "venue"])
    _, _, planning, _ = create_all_agents()
    assert run.delegate(planning, "Recalculer", "planning")["status"] == "reused"
    agent_module.chat.assert_not_called()


@pytest.mark.parametrize("planning", [
    {"start": "2026-12-19", "end": "2026-12-20"},
    {"start": "2026-12-19T20:00", "end": "2026-12-19T02:00"},
    {"start": "2020-12-19T20:00", "end": "2020-12-20T02:00"},
])
def test_incoherent_planning_rejected(planning):
    with pytest.raises(ValueError):
        prepared({}, ["planning"]).validate_planning(encode(planning))


def test_multiday_schedule_does_not_introduce_arbitrary_duration_limit():
    prepared({}, ["planning"]).validate_planning(encode({"start": "2026-12-19T20:00", "end": "2026-12-22T02:00"}))


def test_music_deadline_after_midnight_and_explicit_music_end():
    context = {"planning": {"start": "2026-12-19T20:00", "end": "2026-12-20T03:00"}}
    result = search_candidates("venue", {"ids": ["salle_001"]}, context=context)
    assert result["status"] == "no_match"  # salle_001 : musique jusqu'à 02h00
    context["music_end"] = "2026-12-20T01:30"
    assert search_candidates("venue", {"ids": ["salle_001"]}, context=context)["status"] == "matched"


def test_catering_kitchen_and_lead_time_are_checked():
    catalogs = load_catalogs()
    venue = copy.deepcopy(catalogs["venue"][0])
    venue["equipements"]["cuisine_traiteur"] = False
    context = {"venue": venue, "planning": PLANNING, "today": "2026-11-24"}
    assert search_candidates("catering", {"ids": ["traiteur_001"]}, catalogs=catalogs, context=context)["status"] == "no_match"
    venue["equipements"]["cuisine_traiteur"] = True
    context["today"] = "2026-12-18"
    assert search_candidates("catering", {"ids": ["traiteur_001"]}, catalogs=catalogs, context=context)["status"] == "no_match"


def test_duplicate_search_observation_not_reinjected(monkeypatch):
    search = Mock(return_value={"data": "x" * 5000})
    chats = []

    def fake(messages, tools=None, **kwargs):
        chats.append(copy.deepcopy(messages))
        if len(chats) < 3:
            return message(None, call("search", city="Namur"))
        return message("Terminé")

    monkeypatch.setattr(agent_module, "chat", fake)
    agent = Agent("test", "test", [(search, llm.tool_schema("search", "search", {"city": {"type": "string"}}))])
    assert agent.run("Chercher") == "Terminé"
    search.assert_called_once()
    assert encode(chats[-1]).count("x" * 5000) == 1
    assert any('"status": "reused"' in row.get("content", "") for row in chats[-1])


def test_compact_browse_is_bounded_and_public_fallback_unchanged():
    for category, public in (("venue", tools.get_venues), ("catering", tools.get_caterers), ("provider", tools.get_providers)):
        function, schema = compact_tool(category)
        result = function()
        assert result["shown"] == 5 and result["total"] == len(public())
        assert len(encode(result)) < len(encode(public())) / 5
        assert "Aucun repli" in schema["function"]["description"]


def test_tool_use_failed_has_no_identical_retry(monkeypatch):
    # Construire l'erreur avec le transport effectivement utilisé par le SDK installé.
    import httpx2 as httpx
    error = llm.openai.BadRequestError("tool_use_failed", response=httpx.Response(400, request=httpx.Request("POST", "https://example.test")), body=None)
    client = Mock()
    client.chat.completions.create.side_effect = error
    monkeypatch.setattr(llm, "client", lambda: client)
    with pytest.raises(llm.ErreurLLM, match="400"):
        llm.chat([{"role": "user", "content": "test"}])
    assert client.chat.completions.create.call_count == 1


def test_rate_limit_retries_count_against_shared_limit(monkeypatch):
    import httpx2 as httpx
    error = llm.openai.RateLimitError("429", response=httpx.Response(429, request=httpx.Request("POST", "https://example.test")), body=None)
    client = Mock()
    client.chat.completions.create.side_effect = error
    monkeypatch.setattr(llm, "client", lambda: client)
    monkeypatch.setattr(llm.time, "sleep", Mock())
    monkeypatch.setattr(agent_module, "chat", llm.chat)
    counters = {"llm_calls": 0, "max_llm_calls": 2}
    agent = Agent("test", "test")
    assert "Limite globale" in agent.run("test", counters=counters)
    assert client.chat.completions.create.call_count == 2
    assert counters["llm_calls"] == 2


def test_impossible_planning_leaves_other_tasks_available_without_retry(monkeypatch):
    run = prepared({}, ["planning", "venue", "catering", "provider"])
    _, _, planning, _ = create_all_agents()
    chat = Mock(return_value=message('{"error":"Dates imposées incompatibles avec la durée demandée."}'))
    monkeypatch.setattr(agent_module, "chat", chat)
    result = run.delegate(planning, "Planifier", "planning")
    assert result["status"] == "error" and "Dates imposées" in result["error"]
    assert not run.complete() and not run.successful()
    assert all(run.event_data["tasks"][task]["status"] == "pending" for task in ("venue", "catering", "provider"))
    assert chat.call_count == 1


def test_standalone_caterer_uses_event_area_instead_of_its_head_office(monkeypatch):
    run = prepared({"city": "Namur"}, ["catering"])
    assert run.context("catering")["filters"]["province"] == "Namur"
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", select_first)
    assert run.delegate(decision, "Traiteur à Namur", "catering")["status"] == "success"
    assert "Namur" in run.selected_row("catering")["zones_intervention"]


def test_early_final_response_cannot_claim_success(monkeypatch):
    chat = Mock(return_value=message("Tout est organisé !"))
    monkeypatch.setattr(agent_module, "chat", chat)
    supervisor = create_orchestrator()
    assert "Résultat partiel" in supervisor.run("Organiser une soirée")
    assert supervisor.last_status == "partial" and chat.call_count == 2


def test_a_new_run_does_not_reuse_previous_error_diagnostics(monkeypatch):
    supervisor = create_orchestrator()
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=llm.ErreurLLM("Ancien diagnostic confidentiel")))
    assert "Ancien diagnostic" in supervisor.run("Première demande")
    turns = []

    def fake(messages, tools=None, agent=None):
        if agent == "planning agent":
            return message('{"error":"Nouvelle contrainte impossible"}')
        turns.append(1)
        if len(turns) == 1:
            assert len(messages) == 2 and messages[1]["content"] == "Deuxième demande"
            return message(None, call("planning_agent", demande="Date"))
        return message("Résultat partiel de la deuxième demande")

    monkeypatch.setattr(agent_module, "chat", fake)
    output = supervisor.run("Deuxième demande")
    assert "Ancien diagnostic" not in output and "Nouvelle contrainte impossible" in output
    assert supervisor.event_data["request"] == "Deuxième demande"


def test_failed_final_synthesis_preserves_validated_decisions(monkeypatch):
    def fake(messages, tools=None, agent=None):
        if tools is None and agent == "Supervisor":
            raise llm.ErreurLLM("Service indisponible")
        if agent == "planning agent":
            return message(encode(PLANNING))
        return message(None, call("planning_agent", demande="Date", tasks=["planning"]))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    response = supervisor.run("Propose une date")
    assert "2026-12-19T20:00" in response and "non vérifiées" in response
    assert supervisor.event_data["planning"] == PLANNING


def test_identical_errors_stop_before_iteration_limit(monkeypatch):
    count = []

    def fake(messages, tools=None, **kwargs):
        count.append(tools)
        return message(None, call("unknown")) if tools else message("Résultat partiel : outil inconnu.")

    monkeypatch.setattr(agent_module, "chat", fake)
    agent = Agent("test", "test", [(lambda: None, llm.tool_schema("real_tool", "test", {}))], max_iterations=20)
    assert "partiel" in agent.run("test")
    assert len(count) == 4  # premier échec, deux répétitions sans progrès, puis restitution


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_truncated_provider_response_is_not_promoted(monkeypatch, reason):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=reason, message=message('{"id":"salle_001"}'))])
    monkeypatch.setattr(llm, "client", lambda: client)
    monkeypatch.setattr(llm, "_enregistrer_usage", Mock())
    with pytest.raises(llm.ErreurLLM, match="interrompue"):
        llm.chat([{"role": "user", "content": "test"}])


def test_compatible_choice_not_replaced_by_python(monkeypatch):
    run = prepared({"city": "Namur"}, ["venue"])
    selected = []

    def choose_second(messages, **kwargs):
        candidates = json.loads(messages[1]["content"])["search"]["candidates"]
        assert len(candidates) > 1
        selected.append(candidates[1]["id"])
        return message(encode({"id": selected[-1]}))

    monkeypatch.setattr(agent_module, "chat", choose_second)
    _, _, _, decision = create_all_agents()
    assert run.delegate(decision, "Choisir qualitativement", "venue")["status"] == "success"
    assert run.event_data["venue"]["id"] == selected[0]


def test_prepared_candidates_not_reinjected_when_specialist_searches_again(monkeypatch):
    run = prepared({"city": "Namur"})
    _, _, _, decision = create_all_agents()
    observations = []

    def fake(messages, **kwargs):
        if len(messages) == 2:
            return message(None, call("get_venues", city="Namur", limit=5))
        observations.extend(copy.deepcopy(messages))
        assert json.loads(messages[-1]["content"])["status"] == "reused"
        return select_first(messages)

    monkeypatch.setattr(agent_module, "chat", fake)
    search = Mock(wraps=event_state.search_candidates)
    monkeypatch.setattr(event_state, "search_candidates", search)
    assert run.delegate(decision, "Salle", "venue")["status"] == "success"
    assert search.call_count == 1
    assert "candidates" not in json.loads(observations[-1]["content"])


def test_venue_candidates_exclude_impossible_mandatory_supplier(monkeypatch):
    run = prepared({}, ["venue", "provider"])
    # Deux salles admissibles ; seule la seconde possède un fournisseur autorisé existant.
    first = copy.deepcopy(run.catalogs["venue"][0])
    second = copy.deepcopy(first)
    second["id"] = "salle_solution"
    first["contraintes"]["prestataires_techniques_autorises_ids"] = ["inexistant"]
    second["contraintes"]["prestataires_techniques_autorises_ids"] = ["005"]
    run.catalogs["venue"] = [first, second]
    _, _, _, decision = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", select_first)
    assert run.delegate(decision, "Choisir une salle", "venue")["status"] == "success"
    assert run.event_data["venue"]["id"] == "salle_solution"
    assert run.delegate(decision, "Choisir le fournisseur", "provider")["status"] == "success"
    assert run.event_data["providers"][0]["id"] == "005"


def test_overnight_schedule_and_requested_weekday():
    run = prepared({"weekday": "saturday"}, ["planning"])
    run.validate_planning('{"start":"2026-11-28T20:00","end":"2026-11-29T02:00"}')
    with pytest.raises(ValueError, match="postérieure"):
        run.validate_planning('{"start":"2026-11-28T20:00","end":"2026-11-28T02:00"}')
    with pytest.raises(ValueError, match="jour de semaine"):
        run.validate_planning('{"start":"2026-11-29T20:00","end":"2026-11-30T02:00"}')
    with pytest.raises(ValueError, match="future"):
        prepared({}, ["planning"]).validate_planning(
            '{"start":"2026-11-24T20:00","end":"2026-11-25T02:00"}')


@pytest.mark.parametrize("category", ["venue", "catering", "provider"])
def test_optional_search_filters_can_be_missing_or_null(category):
    assert search_candidates(category)["status"] == "matched"
    assert search_candidates(category, None)["status"] == "matched"
    assert search_candidates(category, {"unknown": None})["status"] == "invalid_filters"


@pytest.mark.parametrize("user_request,requirements", [
    ("Organise une soirée à Namur, sans limite de budget. Donne-moi une date, une salle, un traiteur et un prestataire.",
     {"city": "Namur", "budget_total": None}),
    ("Organise une soirée à Namur pour 100 personnes avec un budget maximum de 5 000 €.",
     {"city": "Namur", "guests": 100, "budget_total": 5000}),
    ("Organise une soirée électronique luxueuse pour 150 personnes à Namur, un samedi soir.",
     {"city": "Namur", "guests": 150, "weekday": "saturday", "preferences": "Électronique, luxueuse"}),
], ids=["scenario_A", "scenario_B", "scenario_C"])
def test_requested_scenarios_complete_with_real_agents_and_mocked_llm(monkeypatch, user_request, requirements):
    phases, agents_seen = [], []
    planning = {"start": "2026-12-19T20:00", "end": "2026-12-20T02:00"}
    next_day = Mock(wraps=tools.get_next_day)
    monkeypatch.setattr("model.agents.agents.get_next_day", next_day)

    def fake(messages, tools=None, agent=None):
        agents_seen.append(agent)
        if agent != "Supervisor":
            payload = json.loads(messages[1]["content"])
            assert payload["today"] == "2026-11-24"
            assert payload["request"] == user_request
        if agent == "planning agent":
            assert "date" not in payload["requirements"]
            if requirements.get("weekday"):
                if len(messages) == 2:
                    return message(None, call("get_next_day", day="saturday", from_date="2026-12-12"))
                assert messages[-1]["content"] == "2026-12-19"
            return message(encode(planning))
        if agent == "budgetary agent":
            assert payload["requirements"]["budget_total"] == 5000
            return message('{"venue":0.45,"catering":0.4,"provider":0.15}')
        if agent == "decision making agent":
            return select_first(messages)
        if tools is None:
            snapshot = messages[-1]["content"]
            assert all(label in snapshot for label in ("Salle :", "Traiteur :", "Prestataire :", "2026-12-20T02:00"))
            return message("Proposition finale :\n" + snapshot.split("\n", 1)[1])
        phases.append(1)
        if len(phases) == 1:
            return message(None, call("planning_agent", demande="Choisir la date et les horaires", requirements=requirements,
                                      tasks=["planning", "venue", "catering", "provider"]))
        if len(phases) == 2:
            calls = [call("budgetary_agent", demande="Répartir le budget")] if requirements.get("budget_total") else []
            return message(None, *calls, call("decision_making_agent", category="venue", demande="Choisir la salle"))
        return message(None, call("decision_making_agent", category="catering", demande="Choisir le traiteur"),
                       call("decision_making_agent", category="provider", demande="Choisir le prestataire"))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    response = supervisor.run(user_request)
    assert supervisor.last_status == "success", response
    assert response.startswith("Proposition finale")
    assert supervisor.event_data["planning"] == planning
    assert all(task["status"] == "success" for task in supervisor.event_data["tasks"].values())
    assert supervisor.event_data["venue"] and supervisor.event_data["catering"] and supervisor.event_data["providers"]
    assert agents_seen.count("decision making agent") == 3
    assert "writer agent" not in agents_seen
    assert ("budgetary agent" in agents_seen) == (requirements.get("budget_total") is not None)
    assert next_day.call_count == (1 if requirements.get("weekday") else 0)
