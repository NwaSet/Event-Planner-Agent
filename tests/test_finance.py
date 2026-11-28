from copy import deepcopy

import pytest

from model.event_state import EventRun
from model.finance import venue_cost, catering_cost, provider_cost, financial_lines
from model.agents.search import load_catalogs, search_candidates


def test_venue_price_is_all_inclusive():
    row = {"prix": {"location_journee_ttc": 1000, "location_soiree_ttc": 1300}}
    costs = venue_cost(row, "soiree")
    assert costs["known_ttc"] == 1300 and costs["deposit"] == 0
    assert costs["cash_required"] == 1300 and not costs["period_assumed"]
    unknown = venue_cost(row)
    assert unknown["period_assumed"] and unknown["known_ttc"] == 1000 and unknown["evening_ttc"] == 1300


def test_catering_rounds_headcount_total_without_logistics():
    costs = catering_cost({}, {"prix_par_personne_ttc": 19.995}, 3)
    assert costs["meal_ttc"] == costs["known_ttc"] == 59.99
    assert catering_cost({}, {"prix_par_personne_ttc": 20})["known_ttc"] is None


@pytest.mark.parametrize("legacy", [{}, {"provider_vat_rate": .21}, {"provider_distance_km": 25, "provider_vehicles": 2}])
def test_provider_ttc_never_adds_tax_or_travel(legacy):
    assert provider_cost({}, {"prix_ttc": 1000}, legacy) == {"kit_ttc": 1000, "known_ttc": 1000}


def test_search_compares_full_technical_ttc_to_budget():
    catalogs = load_catalogs()
    row = deepcopy(catalogs["provider"][4])
    row["kits"] = [{**row["kits"][0], "prix_ttc": 1000}]
    catalogs["provider"] = [row]
    assert search_candidates("provider", {"max_budget": 1000}, catalogs=catalogs)["status"] == "matched"
    assert search_candidates("provider", {"max_budget": 999.99}, catalogs=catalogs)["status"] == "no_match"


def test_all_categories_use_one_ttc_total():
    run = EventRun("Salle, repas et prestataire")
    run.remember({"rental_period": "soiree", "guests": 100})
    run.event_data["results"].update(venue={"id": "salle_001"}, catering={"id": "traiteur_001", "formula_id": "formule_02"}, providers=[{"id": "005", "kit_id": 1}])
    costs = run.costs()
    assert costs["known_ttc_subtotal"] == 2425 + 6400 + 350
    assert costs["known_cash_required"] == costs["known_ttc_subtotal"] and costs["deposit"] == 0
    assert not costs["missing"]
    output = "\n".join(financial_lines(costs))
    assert "9 175,00 EUR" in output and "HT" not in output and "TVA" not in output


@pytest.mark.parametrize("field,value", [("provider_vat_rate", .21), ("provider_distance_km", 25), ("catering_distance_km", 30), ("provider_vehicles", 2)])
def test_obsolete_financial_inputs_do_not_pollute_new_requirements(field, value):
    run = EventRun("Reprendre une ancienne simulation")
    run.remember({field: value})
    assert field not in run.event_data["requirements"]


def test_provider_only_has_a_complete_nonzero_ttc_total():
    run = EventRun("Prestataire")
    run.event_data["results"]["providers"] = [{"id": "005", "kit_id": 1}]
    costs = run.costs()
    before = deepcopy(costs)
    output = "\n".join(financial_lines(costs))
    assert "350,00 EUR TTC" in output
    assert "non déterminable" not in output and "partiel" not in output
    assert costs == before and costs["known_ttc_subtotal"] == 350


def test_unknown_meal_headcount_remains_explicit():
    run = EventRun("Traiteur")
    run.event_data["results"]["catering"] = {"id": "traiteur_001", "formula_id": "formule_02"}
    assert "Effectif du repas à préciser." in run.costs()["missing"]
    assert "non déterminable" in "\n".join(financial_lines(run.costs()))


def test_total_budget_detects_overrun_including_provider_ttc():
    run = EventRun("Prestataire")
    run.remember({"budget_total": 300})
    run.event_data["results"]["providers"] = [{"id": "005", "kit_id": 1}]
    assert run.costs()["known_over_budget"] is True
