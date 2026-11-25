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


def test_final_financial_response_cannot_replace_python_amounts_with_llm_claims(monkeypatch):
    def fake(messages, tools=None, agent=None):
        if agent == "decision making agent":
            return select_first(messages)
        if tools is None:
            return message("Tout est gratuit, total garanti : 0 EUR.")
        return message(None, call("decision_making_agent", demande="Salle", category="venue", tasks=["venue"],
                                  requirements={"venue_filters": {"ids": ["salle_001"], "rental_period": "soiree"}}))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    response = supervisor.run("Salle pour une soirée")
    assert supervisor.last_status == "success"
    assert "gratuit" not in response and "total garanti" not in response
    assert "2 425,00 EUR TTC" in response and "600,00 EUR, hors dépense" in response
    assert "3 025,00 EUR" in response


def test_natural_supervisor_finish_returns_and_traces_only_the_verified_summary(monkeypatch):
    turns = []

    def fake(messages, tools=None, agent=None):
        if agent == "decision making agent":
            return select_first(messages)
        turns.append(1)
        if len(turns) == 1:
            return message(None, call("decision_making_agent", demande="Salle", category="venue",
                                      requirements={"venue_filters": {"ids": ["salle_001"], "rental_period": "soiree"}}))
        return message("Salle inventée gratuite, tout est réservé.")

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    response = supervisor.run("Salle pour une soirée")
    assert supervisor.last_status == "success" and supervisor.counters["llm_calls"] == 3
    assert "Domaine des Brumes Dorées" in response and "2 425,00 EUR TTC" in response
    assert "inventée" not in response and "aucune réservation confirmée" in response
    final_traces = [entry.args[2] for entry in trace.tracer.call_args_list
                    if entry.args[:2] == ("Supervisor", "RESPONSE")]
    assert final_traces == [response]


def test_dictionary_preferences_are_losslessly_normalized_for_all_specialists(monkeypatch):
    preferences = {"ambiance": "étudiante", "musique": {"style": "électro", "volume": "modéré"},
                   "options": ["buffet", "extérieur"], "inconnue": None}
    run = EventRun("Soirée à Namur")
    _, _, planning, _ = create_all_agents()
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message(encode(PLANNING))))
    assert run.delegate(planning, "Date", "planning", requirements={"preferences": preferences})["status"] == "success"
    stored = run.event_data["requirements"]["preferences"]
    assert isinstance(stored, str) and json.loads(stored) == preferences
    assert run.context("planning")["requirements"]["preferences"] == stored
    assert run.context("budget")["requirements"]["preferences"] == stored
    assert run.context("documents")["requirements"]["preferences"] == stored
    for category in ("venue", "catering", "provider"):
        assert run.context(category)["preferences"] == stored
        assert "event_type" not in run.context(category)["filters"]  # préférence ≠ exigence


def test_corrected_preferences_retry_after_first_failure_without_losing_scope(monkeypatch):
    run = EventRun("Date et prestataire")
    _, _, planning, _ = create_all_agents()
    bad = {"preferences": ["électro"]}
    first = run.delegate(planning, "Date", "planning", requirements=bad, tasks=["planning", "provider"])
    assert first["code"] == "invalid_requirements" and first["retryable"]
    assert run.event_data["tasks"]["provider"]["status"] == "pending"
    assert run.delegate(planning, "Date", "planning", requirements=bad,
                        tasks=["planning", "provider"])["status"] == "reused"
    assert run.attempts["planning"]["count"] == 1
    monkeypatch.setattr(agent_module, "chat", Mock(return_value=message(encode(PLANNING))))
    second = run.delegate(planning, "Date", "planning", requirements={"preferences": "électro"})
    assert second["status"] == "success" and run.attempts["planning"]["count"] == 2
    assert run.event_data["planning"] == PLANNING and run.counters["llm_calls"] == 1


def test_changed_relevant_parameters_retry_once_but_rewording_does_not(monkeypatch):
    run = EventRun("Proposer une date")
    _, _, planning, _ = create_all_agents()
    chat = Mock(return_value=message("{}"))
    monkeypatch.setattr(agent_module, "chat", chat)
    assert run.delegate(planning, "Date", "planning", requirements={"preferences": "soir"})["retryable"]
    assert chat.call_count == 2  # une réparation JSON dans la première délégation
    assert run.delegate(planning, "Une autre formulation", "planning")["status"] == "reused"
    assert chat.call_count == 2
    result = run.delegate(planning, "Date", "planning", requirements={"preferences": "après-midi"})
    assert result["status"] == "error" and not result["retryable"]
    assert chat.call_count == 4 and run.counters["delegations"] == 2
    result = run.delegate(planning, "Date", "planning", requirements={"preferences": "matin"})
    assert result["status"] == "reused" and chat.call_count == 4


def test_missing_headcount_can_be_added_to_repair_catering(monkeypatch):
    run = prepared({"city": "Namur", "catering_filters": {"max_budget": 5000}}, ["catering"])
    _, _, _, decision = create_all_agents()
    first = run.delegate(decision, "Traiteur", "catering")
    assert first["status"] == "invalid_filters" and first["retryable"]
    monkeypatch.setattr(agent_module, "chat", select_first)
    second = run.delegate(decision, "Traiteur", "catering", requirements={"catering_filters": {"guests": 40}})
    assert second["status"] == "success"
    assert run.context("catering")["filters"]["max_budget"] == 5000
    assert run.context("catering")["filters"]["guests"] == 40


def test_a_provider_refusal_remains_permanent_even_with_changed_preferences(monkeypatch):
    run = EventRun("Date")
    _, _, planning, _ = create_all_agents()
    chat = Mock(side_effect=llm.ErreurLLM("content_filter"))
    monkeypatch.setattr(agent_module, "chat", chat)
    first = run.delegate(planning, "Date", "planning")
    assert first["code"] == "llm_error" and not first["retryable"]
    assert run.delegate(planning, "Date", "planning", requirements={"preferences": "soir"})["status"] == "reused"
    assert chat.call_count == 1


def test_namur_all_seven_venues_forbid_student_parties():
    all_namur = search_candidates("venue", {"city": "Namur", "limit": 10})
    limited = search_candidates("venue", {"city": "Namur"})
    assert all_namur["total"] == 7 and limited["shown"] == 5 and limited["truncated"]
    assert [row["id"] for row in all_namur["candidates"][5:]] == ["salle_120", "salle_142"]
    assert all("soiree_etudiante" in row["types_evenements_interdits"] for row in all_namur["candidates"])
    filtered = search_candidates("venue", {"city": "Namur", "event_type": "soiree_etudiante"})
    assert filtered["status"] == "no_match" and filtered["total"] == 0 and filtered["candidates"] == []
    assert filtered["filters"]["event_type"] == "soiree_etudiante"


def test_student_party_requirement_reaches_search_even_if_supervisor_only_passes_city(monkeypatch):
    run = EventRun("Organise une soirée étudiante à Namur.")
    _, _, _, decision = create_all_agents()
    result = run.delegate(decision, "Salle", "venue", requirements={"city": "Namur", "preferences": {"ambiance": "luxe"}})
    assert result["status"] == "no_match" and "soirée étudiante" in result["error"]
    assert run.context("venue")["filters"] == {"city": "Namur", "event_type": "soiree_etudiante"}
    assert run.event_data["venue"] is None
    agent_module.chat.assert_not_called()
    with pytest.raises(ValueError, match="Contrainte"):
        run.remember({"event_type": "mariage"})


def test_equivalent_student_type_spellings_do_not_create_false_conflicts():
    run = EventRun("Organise une soirée étudiante à Namur")
    for _ in range(2):
        run.remember({"event_type": "Soirée étudiante", "venue_filters": {"event_type": "soirée étudiante"}})
    assert run.event_data["requirements"]["event_type"] == "soiree_etudiante"
    assert run.context("venue")["filters"]["event_type"] == "soiree_etudiante"


@pytest.mark.parametrize("user_request", ["Organise une soirée à Namur", "Ce n'est pas une soirée étudiante"])
def test_student_category_is_not_invented_for_generic_or_negated_request(user_request):
    assert "event_type" not in EventRun(user_request).event_data["requirements"]


def test_mandatory_constraints_are_all_applied_before_result_limit():
    catalogs = load_catalogs()
    base = copy.deepcopy(catalogs["venue"][0])
    base["lieux"]["commune"] = "Namur"
    base["types_evenements_adaptes"] = ["soiree_etudiante"]
    base["types_evenements_interdits"] = []
    base["capacite"]["maximum_personnes_sur_site"] = 200
    base["prix"].update(location_soiree_ttc=1000, nettoyage_obligatoire_ttc=100)
    base["contraintes"].update(heure_fin_musique="02:00", traiteur_impose=True,
                              traiteurs_autorises_ids=["traiteur_001"], prestataire_technique_impose=True,
                              prestataires_techniques_autorises_ids=["005"])
    rows = [copy.deepcopy(base) for _ in range(8)]
    for index, row in enumerate(rows):
        row["id"] = f"test_{index}"
    rows[0]["types_evenements_interdits"] = ["soiree_etudiante"]  # interdiction prioritaire même si adapté
    rows[1]["capacite"]["maximum_personnes_sur_site"] = 99
    rows[2]["prix"]["location_soiree_ttc"] = 3000
    rows[3]["contraintes"]["heure_fin_musique"] = "22:00"
    rows[4]["contraintes"]["traiteurs_autorises_ids"] = ["traiteur_002"]
    rows[5]["contraintes"]["prestataires_techniques_autorises_ids"] = ["001"]
    catalogs["venue"] = rows
    result = search_candidates("venue", {"city": "Namur", "event_type": "soiree_etudiante", "guests": 100,
                               "max_budget": 2000, "rental_period": "soiree", "caterer_id": "traiteur_001",
                               "provider_id": "005", "limit": 1}, catalogs=catalogs, context={"planning": PLANNING})
    assert result["total"] == 2 and result["shown"] == 1 and result["truncated"]
    assert result["candidates"][0]["id"] == "test_6"


def test_refined_search_cannot_select_a_stale_forbidden_candidate(monkeypatch):
    run = prepared({"city": "Namur"})
    _, _, _, decision = create_all_agents()
    calls = []

    def fake(messages, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return message(None, call("get_venues", event_type="soiree_etudiante"))
        return message('{"id":"salle_010"}')  # existait dans l'ancienne liste, interdit désormais

    monkeypatch.setattr(agent_module, "chat", fake)
    result = run.delegate(decision, "Respecter le type étudiant", "venue")
    assert result["status"] == "error" and result["code"] == "invalid_output"
    assert run.event_data["venue"] is None and len(calls) == 3


def test_repaired_first_task_is_not_stopped_before_supervisor_can_correct_it(monkeypatch):
    turns = []

    def fake(messages, tools=None, agent=None):
        if agent == "planning agent":
            return message(encode(PLANNING))
        if tools is None:
            return message("Date proposée : 19 décembre 2026.")
        turns.append(1)
        return message(None, call("planning_agent", demande="Date", tasks=["planning"],
                                  requirements={"preferences": [] if len(turns) == 1 else {"horaire": "soir"}}))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    supervisor.run("Propose une date")
    assert len(turns) == 2 and supervisor.last_status == "success"
    assert supervisor.event_data["planning"] == PLANNING


def test_student_namur_partial_final_contains_independent_supplier_results(monkeypatch):
    turns, observed = [], []

    def fake(messages, tools=None, agent=None):
        if agent == "planning agent":
            payload = json.loads(messages[1]["content"])
            assert isinstance(payload["requirements"]["preferences"], str)
            raise llm.ErreurLLM("content_filter")
        if agent == "decision making agent":
            payload = json.loads(messages[1]["content"])
            observed.append(payload["task"])
            assert payload["task"] != "venue"  # aucune salle autorisée ne doit atteindre le LLM
            return select_first(messages)
        turns.append(1)
        if len(turns) == 1:
            return message(None, call("planning_agent", demande="Date", requirements={"city": "Namur", "preferences": {"musique": "électro"}},
                                      tasks=["planning", "venue", "catering", "provider"]))
        if len(turns) == 2:
            return message(None, call("decision_making_agent", category="venue", demande="Salle"))
        if len(turns) == 3:
            return message(None, call("decision_making_agent", category="catering", demande="Traiteur indépendant"),
                           call("decision_making_agent", category="provider", demande="Prestataire indépendant"))
        return message("Proposition partielle sans date ni salle.")

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    response = supervisor.run("Organise une soirée étudiante à Namur sans limite de budget. Date, salle, traiteur et prestataire.")
    data = supervisor.event_data
    assert supervisor.last_status == "partial" and data["planning"] is None and data["venue"] is None
    assert data["catering"] and data["providers"]
    assert set(observed) == {"catering", "provider"}
    assert all(data["tasks"][task]["status"] == "success" for task in ("catering", "provider"))
    assert all(text in response for text in ("Traiteur :", "Prestataire :", "Non résolu — planning", "Non résolu — salle", "soirée étudiante"))
    catalogs = load_catalogs()
    for category, selection in (("catering", data["catering"]), ("provider", data["providers"][0])):
        assert next(row["nom"] for row in catalogs[category] if row["id"] == selection["id"]) in response
    assert "future salle à confirmer" in response and "{" not in response


def test_a_direct_planning_without_requirements_or_task_list(monkeypatch):
    request = "Propose une date pour une soirée à Namur."
    rounds = []

    def fake(messages, tools=None, agent=None):
        if agent == "planning agent":
            payload = json.loads(messages[1]["content"])
            assert payload["request"] == request and payload["requirements"] == {"city": "Namur"}
            return message(encode(PLANNING))
        rounds.append(1)
        if len(rounds) == 1:
            assert {schema["function"]["name"] for schema in tools} == {
                "planning_agent", "budgetary_agent", "decision_making_agent", "writer_agent"}
            return message(None, call("planning_agent", demande="Proposer une date et des horaires"))
        return message("Date proposée : 19 décembre 2026, de 20 h à minuit.")

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    assert "19/12/2026 à 20:00" in supervisor.run(request)
    assert supervisor.last_status == "success"
    assert supervisor.event_data["planning"] == PLANNING
    assert supervisor.counters["llm_calls"] == 3


def test_b_direct_venue_specialist_searches_namur_without_configuration(monkeypatch):
    rounds = []

    def fake(messages, tools=None, agent=None):
        if agent == "decision making agent":
            assert [schema["function"]["name"] for schema in tools] == ["get_venues"]
            if len(messages) == 2:
                payload = json.loads(messages[1]["content"])
                assert payload["search"]["filters"] == {"city": "Namur"}
                assert payload["search"]["candidates"]
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
    assert supervisor.counters["llm_calls"] == 3


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
    ("guests", 0), ("budget_total", -1), ("budget_total", ""), ("music_required", "peut-être"),
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
        response = supervisor.run("Organise une soirée à Namur sans limite de budget. Donne-moi une date, une salle, un traiteur et un prestataire.")
        assert all(label in response for label in ("Résultat consolidé", "Salle :", "Traiteur :", "Prestataire :", "Sous-total des postes TTC connus"))
        data = supervisor.event_data
        assert supervisor.last_status == "success"
        assert data["planning"] == PLANNING and data["venue"] and data["catering"] and data["providers"]
        assert data["budget"] is None and not data["documents"]
        assert supervisor.counters["delegations"] == 4
        assert supervisor.counters["llm_calls"] == 7
        assert all(t["status"] == "success" for t in data["tasks"].values())
        if _ == 0:
            previous = data
        else:
            assert previous is not data and previous["tasks"] is not data["tasks"]
    assert len(requests) == 14
    assert all(schemas is not None for agent, _, schemas in requests if agent == "Supervisor")
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
    assert "Résultat partiel" in output and "19/12/2026 à 20:00" in output
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
    assert "19/12/2026 à 20:00" in run.summary("Limite") and run.counters["delegations"] == 1


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


def test_validated_planning_does_not_require_a_final_llm_synthesis(monkeypatch):
    def fake(messages, tools=None, agent=None):
        if tools is None and agent == "Supervisor":
            raise llm.ErreurLLM("Service indisponible")
        if agent == "planning agent":
            return message(encode(PLANNING))
        return message(None, call("planning_agent", demande="Date", tasks=["planning"]))

    chat = Mock(side_effect=fake)
    monkeypatch.setattr(agent_module, "chat", chat)
    supervisor = create_orchestrator()
    response = supervisor.run("Propose une date")
    assert "19/12/2026 à 20:00" in response and "non vérifiées" in response
    assert supervisor.event_data["planning"] == PLANNING
    assert chat.call_count == 2 and supervisor.last_status == "success"


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
    assert response.startswith("Résultat consolidé")
    assert all(label in response for label in ("Salle :", "Traiteur :", "Prestataire :", "Sous-total des postes TTC connus"))
    assert supervisor.event_data["planning"] == planning
    assert all(task["status"] == "success" for task in supervisor.event_data["tasks"].values())
    assert supervisor.event_data["venue"] and supervisor.event_data["catering"] and supervisor.event_data["providers"]
    assert agents_seen.count("decision making agent") == 3
    assert "writer agent" not in agents_seen
    assert ("budgetary agent" in agents_seen) == (requirements.get("budget_total") is not None)
    assert next_day.call_count == (1 if requirements.get("weekday") else 0)


BIRTHDAY_EMAIL = ("Trouve une salle à Namur pour un anniversaire de 100 personnes et rédige un email "
                  "pour demander un devis. Je veux valider le message avant toute utilisation.")


def test_birthday_first_search_retains_city_and_all_structured_constraints(monkeypatch):
    run = EventRun(BIRTHDAY_EMAIL)
    _, _, _, decision = create_all_agents()
    run.remember({"event_type": "anniversaire", "guests": "100", "budget_total": 5000,
                  "venue_filters": {"accessible": True, "rental_period": "soiree"}}, ["venue"])
    run.record("budget", {"status": "success", "result": {"venue": 1, "catering": 0, "provider": 0}})

    def fake(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        filters = payload["search"]["filters"]
        assert filters == {"city": "Namur", "event_type": "anniversaire", "guests": 100,
                           "max_budget": 5000, "accessible": True, "rental_period": "soiree"}
        assert all(row["lieux"]["commune"] == "Namur" for row in payload["search"]["candidates"])
        return select_first(messages)

    chat = Mock(side_effect=fake)
    monkeypatch.setattr(agent_module, "chat", chat)
    assert run.delegate(decision, "Choisir", "venue")["status"] == "success"
    assert chat.call_count == 1
    assert run.event_data["tasks"]["documents"]["status"] == "pending"


@pytest.mark.parametrize("bad", [None, "", "Je choisis cette salle.", '{"id":'])
def test_decision_empty_non_json_or_partial_output_has_one_compact_repair(monkeypatch, bad):
    run = EventRun(BIRTHDAY_EMAIL)
    _, _, _, decision = create_all_agents()
    calls = []

    def fake(messages, tools=None, **kwargs):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            assert tools
            payload = json.loads(messages[1]["content"])
            assert payload["search"]["total"] == payload["search"]["shown"] == 4
            return message(bad)
        assert tools is None
        assert "objet JSON compact" in messages[-1]["content"]
        return select_first(messages)

    monkeypatch.setattr(agent_module, "chat", fake)
    assert run.delegate(decision, "Choisir", "venue")["status"] == "success"
    assert len(calls) == 2


def test_temporary_json_error_is_bounded_but_not_a_permanent_result(monkeypatch):
    run = EventRun(BIRTHDAY_EMAIL)
    _, _, _, decision = create_all_agents()
    chat = Mock(return_value=message("pas du JSON"))
    monkeypatch.setattr(agent_module, "chat", chat)
    failed = run.delegate(decision, "Choisir", "venue")
    assert failed["status"] == "error" and failed["error_kind"] == "temporary_output"
    assert failed["retryable"] and run.event_data["venue"] is None
    repeated = run.delegate(decision, "Encore", "venue")
    assert repeated["status"] == "reused" and repeated["outcome"] == "error"
    assert repeated["error_kind"] == "temporary_output" and repeated["retryable"]
    assert chat.call_count == 2
    chat.side_effect = select_first
    repaired = run.delegate(decision, "Choisir", "venue", requirements={"preferences": "Ambiance élégante"})
    assert repaired["status"] == "success" and chat.call_count == 3
    assert run.delegate(decision, "Encore", "venue")["status"] == "reused"
    assert chat.call_count == 3


@pytest.mark.parametrize("second_reply", ["valid", "invalid", "interrupted", "tools"])
def test_interrupted_decision_gets_at_most_one_repair(monkeypatch, second_reply):
    run = EventRun(BIRTHDAY_EMAIL)
    _, _, _, decision = create_all_agents()
    calls = []

    def fake(messages, tools=None, **kwargs):
        calls.append(1)
        if len(calls) == 1 or second_reply == "interrupted":
            raise llm.SortieInterrompue("Réponse du modèle interrompue : length")
        assert tools is None
        if second_reply == "tools":
            return message(None, call("get_venues", city="Namur"))
        return select_first(messages) if second_reply == "valid" else message("non JSON")

    monkeypatch.setattr(agent_module, "chat", fake)
    result = run.delegate(decision, "Choisir", "venue")
    assert result["status"] == ("success" if second_reply == "valid" else "error")
    assert len(calls) == 2
    if second_reply != "valid":
        assert result["error_kind"] == "temporary_output" and run.event_data["venue"] is None


def test_provider_diagnostics_keep_raw_output_and_reasoning_count(monkeypatch, tmp_path):
    logfile = tmp_path / "logs.jsonl"
    monkeypatch.setattr(llm, "FICHIER_TRACES", logfile)
    monkeypatch.setattr(llm, "JOURNAL", [])
    response = SimpleNamespace(model="modele-simule", choices=[SimpleNamespace(
        finish_reason="tool_calls", message=message("", call("get_venues", city="Namur")))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=8063,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=8000)))
    llm._enregistrer_usage(response, "decision making agent", 0.1)
    entry = json.loads(logfile.read_text(encoding="utf-8"))
    assert entry["finish_reason"] == "tool_calls" and entry["reasoning_tokens"] == 8000
    assert entry["completion_tokens"] == 8063 and entry["modele_retourne"] == "modele-simule"
    assert entry["response"]["content"] == ""
    assert json.loads(entry["response"]["tool_calls"][0]["function"]["arguments"]) == {"city": "Namur"}
    response.usage.completion_tokens_details = None
    response.choices[0].message = message("pas du JSON")
    response.choices[0].finish_reason = "stop"
    llm._enregistrer_usage(response, "decision making agent", 0.1)
    assert llm.JOURNAL[-1]["reasoning_tokens"] is None  # Inconnu, pas zéro supposé.
    assert llm.JOURNAL[-1]["response"]["content"] == "pas du JSON"


@pytest.mark.parametrize("approved", [True, False])
def test_birthday_venue_then_writer_then_human_approval_without_sending(monkeypatch, approved):
    stages = []
    draft = {}
    approval = Mock(return_value={"approved": approved})
    monkeypatch.setattr(tools, "ask_user", approval)

    def fake(messages, tools=None, agent=None):
        stages.append(agent)
        assert all("send" not in tool["function"]["name"] for tool in tools or [])
        if agent == "decision making agent":
            payload = json.loads(messages[1]["content"])
            assert payload["filters"] == {"city": "Namur", "guests": 100, "event_type": "anniversaire"}
            assert payload["search"]["total"] == 4
            return select_first(messages)
        if agent == "writer agent":
            payload = json.loads(messages[1]["content"])
            assert payload["request"] == BIRTHDAY_EMAIL
            assert payload["venue"]["nom"] and payload["venue"]["lieux"]["commune"] == "Namur"
            assert "ne pas inventer" in payload["contact_note"]
            if len(messages) == 2:
                assert [tool["function"]["name"] for tool in tools] == ["ask_user"]
                draft["text"] = "Objet : Demande de devis\nBonjour " + payload["venue"]["nom"] + ",\nDevis pour un anniversaire de 100 personnes à Namur ?\nMerci."
                return message(None, call("ask_user", message=draft["text"]))
            return message(draft["text"])
        if stages.count("Supervisor") == 1:
            schema = next(tool for tool in tools if tool["function"]["name"] == "decision_making_agent")
            assert "city" in schema["function"]["parameters"]["properties"]["requirements"]["properties"]
            # Reproduction : ville et documents omis par le Supervisor.
            return message(None, call("decision_making_agent", category="venue", demande="Salle",
                                      requirements={"event_type": "anniversaire", "guests": 100}, tasks=["venue"]))
        return message(None, call("writer_agent", demande="Rédige le devis demandé"))

    monkeypatch.setattr(agent_module, "chat", fake)
    supervisor = create_orchestrator()
    output = supervisor.run(BIRTHDAY_EMAIL)
    approval.assert_called_once_with(draft["text"])
    assert stages == ["Supervisor", "decision making agent", "Supervisor", "writer agent", "writer agent"]
    assert supervisor.event_data["venue"]
    assert supervisor.event_data["documents"] == [{"content": draft["text"], "approved": approved}]
    assert supervisor.last_status == ("success" if approved else "partial")
    assert draft["text"] in output and "Aucun email envoyé" in output


def test_email_cannot_invent_venue_when_selection_failed(monkeypatch):
    run = EventRun(BIRTHDAY_EMAIL)
    _, writer, _, decision = create_all_agents()
    assert run.delegate(writer, "Email", "documents")["requires"] == "venue"
    result = run.delegate(decision, "Salle", "venue", requirements={"venue_filters": {"ids": []}})
    assert result["status"] == "no_match" and result["error_kind"] == "business_impossibility"
    approval = Mock()
    monkeypatch.setattr(tools, "ask_user", approval)
    assert run.delegate(writer, "Email", "documents")["status"] == "blocked"
    assert run.delegate(writer, "Email", "documents")["status"] == "reused"
    assert run.counters["llm_calls"] == 0 and not run.event_data["documents"]
    assert "destinataire inconnu" in run.summary("Sélection impossible")
    approval.assert_not_called()


def test_literal_city_case_and_negated_email_do_not_create_false_requirements():
    run = EventRun("Trouve une salle à Namur. Ne rédige pas un email.")
    run.remember({"city": "namur"})
    assert run.context("venue")["filters"]["city"] == "Namur"
    assert "documents" not in run.event_data["tasks"]
    assert "city" not in EventRun("Trouve une salle, pas à Namur.").event_data["requirements"]


@pytest.mark.parametrize("city", [100, {"ville": "Namur"}])
def test_invalid_city_type_does_not_break_the_run_or_drop_other_tasks(city):
    run = EventRun(BIRTHDAY_EMAIL)
    _, _, _, decision = create_all_agents()
    outcome = run.delegate(decision, "Salle", "venue", requirements={"city": city})
    assert outcome["code"] == "invalid_requirements"
    assert run.event_data["requirements"]["city"] == "Namur"
    assert run.event_data["tasks"]["documents"]["status"] == "pending"
    assert run.counters["llm_calls"] == 0
