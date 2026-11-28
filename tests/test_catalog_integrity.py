"""Cohérence des trois catalogues fictifs, sans réseau ni modèle réel."""
import copy
import math
import re
from datetime import datetime

import pytest

from model.agents import tools
from model.agents.search import load_catalogs, search_candidates
from model.event_state import EventRun


def positive_price(value):
    assert type(value) in (int, float) and math.isfinite(value) and value > 0
    assert round(value, 2) == value


def test_venues_have_only_simple_fields_and_no_supplier_links():
    for row in load_catalogs()["venue"]:
        assert set(row) == {"id", "nom", "lieux", "capacite", "type_espace", "prix",
                            "types_evenements_adaptes", "equipements", "heure_fin_musique"}
        assert type(row["capacite"]) is int and row["capacite"] > 0
        assert row["type_espace"] in {"interieur", "exterieur", "les deux"}
        assert set(row["prix"]) == {"location_journee_ttc", "location_soiree_ttc"}
        for price in row["prix"].values():
            positive_price(price)
        datetime.strptime(row["heure_fin_musique"], "%H:%M")
        assert row["types_evenements_adaptes"]
        assert len(row["equipements"]) == len(set(row["equipements"]))
        assert set(row["equipements"]) <= {"bar", "piste_de_danse", "sonorisation_incluse",
                                           "eclairage_festif_inclus", "videoprojecteur"}


def test_formulas_agree_on_meal_capacity_price_and_dietary_options():
    for row in load_catalogs()["catering"]:
        assert len(row["options_alimentaires"]) == len(set(row["options_alimentaires"]))
        assert set(row["options_alimentaires"]) <= {"vegetarien", "vegan", "sans_gluten", "sans_lactose", "halal", "casher"}
        if "vegan" in row["options_alimentaires"]:
            assert "vegetarien" in row["options_alimentaires"]
        assert len({f["id"] for f in row["formules"]}) == len(row["formules"])
        assert any(f["repas_complet"] for f in row["formules"])
        for formula in row["formules"]:
            positive_price(formula["prix_par_personne_ttc"])
            assert type(formula["maximum_personnes"]) is int and formula["maximum_personnes"] >= 250
            assert type(formula["repas_complet"]) is bool
            assert formula["repas_complet"] == (formula["type_service"] != "cocktail")
            if "ne remplace pas un repas" in formula["description"].lower():
                assert not formula["repas_complet"]


def test_kits_share_ttc_prices_and_technical_levels_without_contradictory_absence():
    for row in load_catalogs()["provider"]:
        assert len({k["id"] for k in row["kits"]}) == len(row["kits"])
        for kit in row["kits"]:
            assert set(kit) == {"id", "description", "son", "light", "video", "prix_ttc"}
            positive_price(kit["prix_ttc"])
            assert all(kit[field] in tools.LEVELS for field in ("son", "light", "video"))
            text = tools._normalize(kit["description"])
            for word, field in (("video", "video"), ("son", "son"), ("eclairage", "light")):
                if re.search(r"\bsans " + word + r"\b", text):
                    assert kit[field] == "absent"


def test_shared_communes_never_have_conflicting_provinces():
    catalogs = load_catalogs()
    provinces = {}
    for row in catalogs["venue"] + catalogs["catering"]:
        city = tools._normalize(row["lieux"]["commune"])
        province = tools._normalize(row["lieux"]["province"])
        assert provinces.setdefault(city, province) == province
    for row in catalogs["provider"]:
        if row.get("province"):
            declared = {tools._normalize(p) for p in row["province"].split(" / ")}
            for city in tools.row_cities(row):
                if tools._normalize(city) in provinces:
                    assert provinces[tools._normalize(city)] in declared


@pytest.mark.parametrize("category,count", [("venue", 150), ("catering", 100), ("provider", 40)])
def test_unique_ids_nonempty_names_and_locations(category, count):
    rows = load_catalogs()[category]
    assert len(rows) == len({r["id"] for r in rows}) == count
    assert all(isinstance(r["id"], str) and r["nom"].strip() and tools.row_cities(r) for r in rows)


def test_old_pmr_seating_tax_and_supplier_constraints_do_not_return_on_resume():
    run = EventRun("Événement", interactive=True)
    saved = copy.deepcopy(run.event_data)
    saved["requirements"] = {"accessible": True, "seated": True, "provider_vat_rate": .21,
                              "venue_constraints": {"accessible": True, "seated": True, "provider_id": "002"}}
    saved["results"]["venue"] = {"id": "salle_001"}
    original = copy.deepcopy(saved)
    resumed = EventRun("Reprendre", event_data=saved, interactive=True)
    assert saved == original
    assert resumed.event_data["requirements"] == {}
    assert resumed.event_data["results"]["venue"] == {"id": "salle_001"}
    assert "accessible" not in resumed.context("venue")["filters"]
    assert "contraintes" not in resumed.selection_details("venue")
    assert search_candidates("provider", {"ids": ["030"]}, context={"venue": resumed.selected_row("venue")})["status"] == "matched"
