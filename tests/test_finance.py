from copy import deepcopy

import pytest

from model.event_state import EventRun
from model.finance import venue_cost, catering_cost, provider_cost, financial_lines, euros
from model.agents.search import load_catalogs, search_candidates


def test_evening_cleaning_and_refundable_deposit_are_separate():
    row = {"prix": {"location_journee_ttc": 1000, "location_soiree_ttc": 1300,
                    "nettoyage_obligatoire_ttc": 75, "caution_remboursable": 600}}
    costs = venue_cost(row, "soiree")
    assert costs["known_ttc"] == 1375 and costs["deposit"] == 600
    assert costs["cash_required"] == 1975 and not costs["period_assumed"]
    unknown = venue_cost(row)
    assert unknown["period_assumed"] and unknown["known_ttc"] == 1075 and unknown["evening_ttc"] == 1375


def test_catering_rounds_lines_at_the_cent_and_adds_trip_to_delivery():
    row = {"frais_supplementaires": {"livraison_forfait_ttc": 10, "deplacement_eur_ttc_km": .65}}
    costs = catering_cost(row, {"prix_par_personne_ttc": 19.995}, 3, 11)
    assert costs["meal_ttc"] == 59.99 and costs["travel_ttc"] == 7.15
    assert costs["known_ttc"] == 77.14
    assert catering_cost(row, {"prix_par_personne_ttc": 20})["known_ttc"] is None


def test_provider_travel_counts_vehicles_then_vat_on_kit_and_travel():
    row = {"deplacement": {"prix_simule_eur_ht_km_par_vehicule": .735}}
    costs = provider_cost(row, {"prix_estime_eur_ht": 1000},
                          {"provider_distance_km": 10.5, "provider_vehicles": 2, "provider_vat_rate": .21})
    assert costs["travel_ht"] == 15.44 and costs["known_ht"] == 1015.44
    assert costs["vat"] == 213.24 and costs["known_ttc"] == 1228.68
    unknown = provider_cost(row, {"prix_estime_eur_ht": 1000}, {})
    assert unknown["travel_ht"] is None and unknown["vat"] is None and unknown["known_ttc"] is None
    zero = provider_cost(row, {"prix_estime_eur_ht": 1000}, {"provider_distance_km": 0, "provider_vat_rate": 0})
    assert zero["travel_ht"] == 0 and zero["vat"] == 0 and zero["known_ttc"] == 1000


def test_search_rejects_kit_whose_vat_and_travel_exceed_budget():
    catalogs = load_catalogs()
    row = deepcopy(catalogs["provider"][4])
    row["kits"] = [{**row["kits"][0], "prix_estime_eur_ht": 1000}]
    row["deplacement"]["prix_simule_eur_ht_km_par_vehicule"] = .735
    catalogs["provider"] = [row]
    filters = {"ids": [row["id"]], "max_budget": 1100}
    assert search_candidates("provider", filters, catalogs=catalogs)["status"] == "matched"
    checked = search_candidates("provider", filters, catalogs=catalogs, context={"financial": {
        "provider_distance_km": 10.5, "provider_vehicles": 2, "provider_vat_rate": .21}})
    assert checked["status"] == "no_match" and not checked["candidates"]


def test_subtotal_never_adds_unknown_vat_ht_to_ttc_or_deposit_to_expenses():
    run = EventRun("Salle et prestataire")
    run.event_data["venue"] = {"id": "salle_001"}
    run.event_data["providers"] = [{"id": "005", "kit_id": 1}]
    costs = run.costs()
    assert costs["known_ttc_subtotal"] == costs["known"]["venue_ttc"]
    assert costs["known_cash_required"] == costs["known_ttc_subtotal"] + costs["deposit"]
    assert costs["details"]["provider"]["vat"] is None
    assert any("TVA" in warning for warning in costs["missing"])
    assert any("kilométrage" in warning.lower() for warning in costs["missing"])


@pytest.mark.parametrize("field,value", [("provider_vat_rate", 21), ("provider_vat_rate", True),
                                        ("provider_vehicles", 0), ("provider_vehicles", 1.5),
                                        ("provider_distance_km", -1), ("catering_distance_km", float("nan"))])
def test_invalid_financial_inputs_are_rejected(field, value):
    with pytest.raises(ValueError):
        EventRun("test").remember({field: value})


def test_provider_only_unknown_vat_and_travel_never_displays_a_zero_total():
    run = EventRun("Prestataire")
    run.event_data["providers"] = [{"id": "005", "kit_id": 1}]
    costs = run.costs()
    before = deepcopy(costs)
    output = "\n".join(financial_lines(costs))
    assert euros(costs["known"]["provider_ht"]) + " HT" in output
    assert "TVA du prestataire inconnue" in output and "exclus du sous-total TTC" in output
    assert "Kilométrage facturable et nombre de véhicules" in output
    assert "Aucun sous-total TTC calculable" in output
    assert "Coût TTC total : non déterminable" in output
    assert "mobiliser" not in output and ": 0,00 EUR" not in output
    assert costs == before and costs["known_ttc_subtotal"] == 0  # calculs inchangés


def test_mixed_ht_ttc_cash_and_deposit_are_explicitly_partial():
    run = EventRun("Salle et prestataire")
    run.remember({"rental_period": "soiree"})
    run.event_data["venue"] = {"id": "salle_001"}
    run.event_data["providers"] = [{"id": "005", "kit_id": 1}]
    costs = run.costs()
    output = "\n".join(financial_lines(costs))
    assert "Sous-total des postes TTC connus (partiel) : " + euros(costs["known"]["venue_ttc"]) in output
    assert "Caution remboursable : 600,00 EUR, hors dépense" in output
    assert "Somme partielle à mobiliser sur ces seuls postes, caution comprise : 3 025,00 EUR" in output
    assert "Coût TTC total : non déterminable" in output
    assert costs["known_ttc_subtotal"] == 2425 and costs["deposit"] == 600


def test_known_provider_vat_still_reports_unknown_travel_and_partial_ttc():
    run = EventRun("Prestataire")
    run.remember({"provider_vat_rate": .21})
    run.event_data["providers"] = [{"id": "005", "kit_id": 1}]
    output = "\n".join(financial_lines(run.costs()))
    assert "TVA au taux fourni de 21 %" in output
    assert "Sous-total des postes TTC connus (partiel)" in output
    assert "Coût TTC total : non déterminable" in output
    assert "Kilométrage" in output
