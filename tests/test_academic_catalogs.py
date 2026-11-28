"""Dix scénarios académiques sur les vrais JSON, LLM et géocodage simulés."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from controller.event_controller import run_conversation, continue_conversation
from dao.conversation_dao import load_conversation
from model import agent as agent_module, trace
from model.agents import create_all_agents, tools
from model.agents.search import load_catalogs, search_candidates
from model.event_orchestrator import create_orchestrator
from model.event_state import EventRun


@pytest.fixture(autouse=True)
def simulated_only(monkeypatch):
    monkeypatch.setattr(trace, "tracer", Mock())
    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=AssertionError("LLM non simulé")))


SCENARIOS = [
    ("soiree_namur_100", "Soirée à Namur pour 100 personnes", "Namur", 100, "soiree", {}, {}, {}),
    ("etudiants_200", "Soirée étudiante pour 200 personnes à Namur", "Namur", 200, "soiree_etudiante", {}, {}, {}),
    ("afterwork_anvers", "Afterwork à Anvers pour 100 personnes", "Anvers", 100, "afterwork", {}, {}, {}),
    ("mariage_150", "Mariage pour 150 personnes à Namur", "Namur", 150, "mariage", {}, {}, {}),
    ("exterieur_souple", "Événement à Namur, extérieur si possible", "Namur", 100, "fete_associative", {}, {}, {}),
    ("sans_plafond", "Anniversaire à Namur sans limite budgétaire", "Namur", 100, "anniversaire", {}, {}, {}),
    ("budget_limite", "Soirée à Namur avec budget limité", "Namur", 100, "soiree", {"max_budget": 2000}, {"max_budget": 3500}, {"max_budget": 1500}),
    ("repas_complet", "Soirée à Namur avec repas complet obligatoire", "Namur", 100, "soiree", {}, {"full_meal": True}, {}),
    ("tres_bon_son", "Soirée à Namur, très bon système sonore obligatoire", "Namur", 100, "soiree", {}, {}, {"sound_level": "très bien"}),
    ("absence_reelle", "Soirée à Namur pour 1000000 personnes", "Namur", 1000000, "soiree", {}, {}, {}),
]


@pytest.mark.parametrize("name,user_request,city,guests,event,venue,catering,provider", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_ten_scenarios_keep_comparison_and_strict_failure(monkeypatch, name, user_request, city, guests, event, venue, catering, provider):
    run = EventRun(user_request, interactive=True)
    requirements = {"city": city, "guests": guests, "event_type": event,
                    "venue_constraints": venue, "catering_constraints": catering,
                    "provider_constraints": provider, "rental_period": "soiree"}
    if name == "exterieur_souple":
        requirements["preferences"] = "Extérieur si possible"
    run.remember(requirements, ["venue", "catering", "provider"])
    _, _, _, decision = create_all_agents()
    comparisons = []

    def choose(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        rows = payload["search"]["candidates"]
        assert len(rows) >= 2, (name, payload["task"], payload["search"])
        # The simulated model deliberately chooses a later candidate, never Python's first.
        row = rows[-1]
        comparisons.append((payload["task"], len(rows), row["id"]))
        selected = {"id": row["id"]}
        if payload["task"] == "catering":
            if name == "repas_complet":
                assert all(f["repas_complet"] for r in rows for f in r["formules"])
            selected["formula_id"] = row["formules"][-1]["id"]
        elif payload["task"] == "provider":
            if name == "tres_bon_son":
                assert all(k["son"] == "excellent" for r in rows for k in r["kits"])
            selected["kit_id"] = row["kits"][-1]["id"]
        if name == "exterieur_souple" and payload["task"] == "venue":
            assert "space_type" not in payload["filters"]
            assert {r["type_espace"] for r in rows} >= {"interieur", "exterieur"}
        return SimpleNamespace(content=json.dumps(selected), tool_calls=None)

    monkeypatch.setattr(agent_module, "chat", Mock(side_effect=choose))
    result = run.delegate(decision, "Comparer les salles", "venue")
    if name == "absence_reelle":
        assert result["status"] == "no_match"
        agent_module.chat.assert_not_called()
        assert "Non résolu" in run.summary("")
        return
    assert result["status"] == "success", result
    for category in ("catering", "provider"):
        result = run.delegate(decision, "Comparer les offres", category)
        assert result["status"] == "success", result
    assert len(comparisons) == 3 and run.successful()
    if name == "budget_limite":
        assert run.costs()["known"]["venue_ttc"] <= 2000
        assert run.costs()["known"]["catering_ttc"] <= 3500
        assert run.costs()["known"]["provider_ttc"] <= 1500
    if name == "sans_plafond":
        assert all("max_budget" not in run.context(c)["filters"] for c in ("venue", "catering", "provider"))


def test_soft_outdoor_preference_is_not_promoted_to_nested_filter():
    run = EventRun("Un événement à Namur, extérieur si possible", interactive=True)
    cleaned = run.supervisor_requirements({"space_type": "exterieur", "venue_constraints": {"space_type": "exterieur"}})
    assert "space_type" not in cleaned and "venue_constraints" not in cleaned
    assert "extérieur" in cleaned["preferences"]
    strict = EventRun("Un événement obligatoirement en extérieur", interactive=True)
    assert strict.supervisor_requirements({"venue_constraints": {"space_type": "exterieur"}})["venue_constraints"]["space_type"] == "exterieur"


def test_required_equipment_and_specialized_venue_are_enforced():
    options = search_candidates("venue", {"city": "Namur", "equipment": ["piste_de_danse", "bar"]})
    assert options["status"] == "matched"
    assert all("piste_de_danse" in r["equipements"] and "bar" in r["equipements"] for r in options["candidates"])
    assert search_candidates("venue", {"equipment": ["equipement_inexistant"]})["status"] == "no_match"
    assert search_candidates("venue", {"ids": ["salle_008"], "event_type": "soiree_etudiante"})["status"] == "no_match"
    venue = load_catalogs()["venue"][0]
    assert search_candidates("provider", {"ids": ["030"]}, context={"venue": venue})["status"] == "matched"


def test_neighboring_services_can_cross_province_without_logistic_checks(monkeypatch):
    catalogs = load_catalogs()
    venue = catalogs["venue"][9]  # Namur, no imposed supplier.
    other = copy.deepcopy(catalogs["catering"][0])  # Brussels.
    catalogs["catering"] = [other]
    rank = Mock(return_value=["Bruxelles"])
    monkeypatch.setattr(tools, "select_filtered", rank)
    result = search_candidates("catering", {"full_meal": True, "guests": 100}, catalogs=catalogs,
                               context={"venue": venue, "date": "2027-01-02", "today": "2027-01-01"})
    assert result["status"] == "matched" and result["location_search"]["selected_city"] == "Bruxelles"
    rank.assert_called_once()
    assert "province" not in result["filters"]
    strict = search_candidates("catering", {"province": "Namur"}, catalogs=catalogs, context={"venue": venue})
    assert strict["status"] == "no_match"  # An explicitly imposed location is still mandatory.


def message(content=None, *calls):
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


def call(name, **args):
    return SimpleNamespace(id=name, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def test_all_five_agents_finance_writer_approvals_and_saved_ids(monkeypatch, tmp_path):
    observed = []
    stages = iter(["requirements", "delegate"])
    schedule = {"start": "2027-06-12T19:00", "end": "2027-06-13T01:00"}
    draft = "Objet : Repas complet\nMerci de confirmer la disponibilité de la formule proposée."

    def simulate(messages, agent=None, **kwargs):
        observed.append(agent)
        payload = json.loads(messages[1]["content"])
        if agent == "Supervisor":
            if next(stages) == "requirements":
                return message(None, call("revise_event", requirements={"city": "Namur", "guests": 100,
                    "event_type": "soiree", "budget_total": 7000, "rental_period": "soiree",
                    "catering_constraints": {"full_meal": True}},
                    tasks=["planning", "budget", "venue", "catering", "provider", "documents"]))
            return message(None, call("planning_agent", instruction="Date et horaires"),
                           call("budgetary_agent", instruction="Répartir 7000 euros"),
                           *(call("decision_making_agent", category=c, instruction="Comparer les candidats") for c in ("venue", "catering", "provider")),
                           call("writer_agent", instruction="Demande de devis au traiteur"))
        if agent == "planning agent":
            return message(json.dumps(schedule))
        if agent == "budgetary agent":
            return message(json.dumps({"venue": .3, "catering": .5, "provider": .2}))
        if agent == "decision making agent":
            rows = payload["search"]["candidates"]
            assert len(rows) >= 2
            row = rows[-1]
            result = {"id": row["id"]}
            if payload["task"] == "catering":
                result["formula_id"] = row["formules"][-1]["id"]
            if payload["task"] == "provider":
                result["kit_id"] = row["kits"][-1]["id"]
            return message(json.dumps(result))
        assert agent == "writer agent" and payload["plan_approved"] is True
        assert payload["catering"]["offer"]["repas_complet"] is True
        assert payload["costs"]["details"]["catering"]["known_ttc"] == payload["catering"]["offer"]["prix_par_personne_ttc"] * 100
        assert not {"reservation", "deplacement", "frais_supplementaires"} & payload["catering"].keys()
        return message(None, call("ask_user", message=draft))

    monkeypatch.setattr(agent_module, "chat", simulate)
    request = "Soirée à Namur pour 100 personnes, budget total 7000 euros, repas complet obligatoire. Rédige un email au traiteur."
    conversation = run_conversation(request, directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert conversation["event_data"]["workflow"]["pending_input"]["kind"] == "plan_approval"
    assert "writer agent" not in observed
    selections = copy.deepcopy(conversation["event_data"]["results"])
    costs = EventRun("Consulter les montants", event_data=conversation["event_data"], interactive=True).costs()
    assert costs["known_ttc_subtotal"] <= 7000 and not costs["missing"]
    assert "déplacement" not in conversation["messages"][-1]["content"].lower()
    conversation = continue_conversation(load_conversation(conversation["conversation_id"], tmp_path), "oui", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert conversation["event_data"]["workflow"]["pending_input"]["kind"] == "email_approval"
    count = len(observed)
    conversation = continue_conversation(conversation, "oui", directory=tmp_path, orchestrator_factory=create_orchestrator)
    assert len(observed) == count and conversation["status"] == "completed"
    assert set(observed) == {"Supervisor", "planning agent", "budgetary agent", "decision making agent", "writer agent"}
    assert conversation["event_data"]["results"]["documents"][0]["content"] == draft
    for category in ("venue", "catering", "providers"):
        assert conversation["event_data"]["results"][category] == selections[category]


def test_supervisor_reports_real_no_match_without_a_selection(monkeypatch):
    stages = iter([
        message(None, call("revise_event", requirements={"city": "Namur", "guests": 1000000, "event_type": "soiree"}, tasks=["venue"])),
        message(None, call("decision_making_agent", category="venue", instruction="Salle pour 1000000 personnes")),
    ])
    def simulate(messages, agent=None, **kwargs):
        assert agent == "Supervisor"
        return next(stages)
    monkeypatch.setattr(agent_module, "chat", simulate)
    supervisor = create_orchestrator()
    text = supervisor.run("Soirée à Namur pour 1000000 personnes", interactive=True)
    assert supervisor.event_data["tasks"]["venue"]["status"] == "no_match"
    assert supervisor.event_data["results"]["venue"] is None and "Non résolu" in text


def test_catalog_ids_offer_ids_counts_and_level_vocabulary():
    catalogs = load_catalogs()
    for category, count, template in (("venue", 150, "salle_{:03}"), ("catering", 100, "traiteur_{:03}"), ("provider", 40, "{:03}")):
        assert [row["id"] for row in catalogs[category]] == [template.format(i) for i in range(1, count + 1)]
    assert sum(len(row["formules"]) for row in catalogs["catering"]) == 322
    assert all([f["id"] for f in row["formules"]] == [f"formule_{i:02}" for i in range(1, len(row["formules"]) + 1)] for row in catalogs["catering"])
    assert all([k["id"] for k in row["kits"]] == list(range(1, 7)) for row in catalogs["provider"])
    assert all(k[field] in tools.LEVELS for row in catalogs["provider"] for k in row["kits"] for field in ("son", "light", "video"))
    assert all(not {"logistique", "reservation", "frais_supplementaires", "zones_intervention"} & row.keys() for row in catalogs["catering"])
    assert all(not {"deplacement", "adresse", "categorie_taille"} & row.keys() for row in catalogs["provider"])
