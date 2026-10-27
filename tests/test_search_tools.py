"""Offline geography and catalogue checks using real project data."""

import json
import math

import pytest

from tools import data_tools, search_tools as search


def test_sourced_coordinates_and_aliases():
    cities = search._cities()
    assert cities["namur"]["latitude"] == 50.4669
    assert cities["5030"] == cities["gembloux"]
    assert cities["anvers"] == cities["antwerpen"]
    assert cities["alost"]["postal_code"] == "9300"
    assert cities["halle"]["postal_code"] == "1500"
    for city in cities.values():
        assert 49 < city["latitude"] < 52 and 2 < city["longitude"] < 7
    source = json.loads((data_tools._DATA_DIR / "cities.json").read_text(encoding="utf-8"))
    assert source["meta"]["url"].startswith("https://download.geonames.org/")
    assert source["meta"]["license"] == "CC BY 4.0"


def test_haversine_known_distances_and_symmetry():
    assert search.haversine(50, 4, 50, 4) == 0
    assert search.haversine(0, 0, 0, 1) == pytest.approx(111.195, abs=.001)
    distance = search.haversine(50.4669, 4.8675, 50.5615, 4.6989)
    assert 15 < distance < 17
    assert distance == search.haversine(50.5615, 4.6989, 50.4669, 4.8675)
    assert math.isfinite(search.haversine(0, 0, 0, 180))


@pytest.mark.parametrize("coordinates", [(91, 0, 0, 0), (0, -181, 0, 0), (True, 0, 0, 0), (float("nan"), 0, 0, 0)])
def test_invalid_coordinates_fail(coordinates):
    with pytest.raises(ValueError):
        search.haversine(*coordinates)


def test_namur_search_uses_entire_catalogue_and_price_includes_cleaning():
    result = search.search_venues(city="Namur", radius_km=30, min_capacity=200, max_price=2000)
    assert result["returned"] == 3
    assert result["candidates"][0]["id"] == "salle_054"  # Outside the former first page.
    for candidate in result["candidates"]:
        source = data_tools.get_venue(candidate["id"])
        assert candidate["capacity"] >= 200 and candidate["distance_km"] <= 30
        assert candidate["price_eur"] == source["prix"]["location_journee_ttc"] + source["prix"]["nettoyage_obligatoire_ttc"]
        assert candidate["price_eur"] <= 2000 and candidate["tax_basis"] == "TTC"
    assert search.search_venues(city="Namur", radius_km=0)["total_matches"] > 0
    assert all(c["distance_km"] == 0 for c in search.search_venues(city="Namur", radius_km=0)["candidates"])


def test_venue_configuration_event_accessibility_and_limit():
    result = search.search_venues(space="outdoor", configuration="standing", rental="evening",
                                  event_type="mariage", accessible=True, min_capacity=150, limit=20)
    assert 0 < result["returned"] <= 20
    for candidate in result["candidates"]:
        source = data_tools.get_venue(candidate["id"])
        assert "mariage" not in source["types_evenements_interdits"]
        assert source["equipements"]["acces_pmr"] is True
        assert candidate["capacity"] == min(source["capacite"]["exterieur"]["personnes_debout"], source["capacite"]["maximum_personnes_sur_site"])
        assert candidate["price_eur"] == source["prix"]["location_soiree_ttc"] + source["prix"]["nettoyage_obligatoire_ttc"]


def test_geography_normalizes_accents_aliases_and_reports_unknown_locations():
    assert search.search_venues(city="Liège")["candidates"] == search.search_venues(city="LIEGE")["candidates"]
    assert search.search_venues(city="Anvers")["candidates"] == search.search_venues(city="Antwerpen")["candidates"]
    unknown = search.search_venues(city="Atlantis")
    assert unknown["error"] and unknown["missing_coordinates"] == ["Atlantis"]
    assert unknown["candidates"] == []
    providers = search.search_providers(city="Gembloux", radius_km=30)
    assert {"Andrimont", "Nazareth-De Pinte"} <= set(providers["missing_coordinates"])
    assert any(c["location"] == "Gembloux / Wasseiges" and c["distance_km"] == 0 for c in providers["candidates"])


def test_no_automatic_radius_expansion():
    result = search.search_venues(city="Gembloux", radius_km=1)
    assert result["returned"] == 0 and result["radius_km"] == 1
    assert result["suggested_radius_km"] == 2


def test_caterer_formula_constraints_and_dietary_requirements():
    result = search.search_caterers(city="Gembloux", attendance=100, max_price_per_person=70,
                                    service_type="buffet", full_meal=True, dietary=["vegetarien"])
    assert result["returned"] > 0
    for candidate in result["candidates"]:
        assert "vegetarien" in candidate["dietary"]
        for formula in candidate["formulas"]:
            assert formula["service"] == "buffet" and formula["full_meal"]
            assert formula["attendance"][0] <= 100 <= formula["attendance"][1]
            assert formula["price_per_person"] <= 70
    assert search.search_caterers(dietary=["not_a_diet"])["returned"] == 0


def test_technical_kit_constraints_are_distinct_from_catering():
    result = search.search_providers(city="Gembloux", services=["son", "light"], max_kit_price=1000)
    assert result["returned"] > 0
    for candidate in result["candidates"]:
        assert isinstance(candidate["id"], str) and candidate["tax_basis"] == "HT"
        for kit in candidate["kits"]:
            assert kit["son"] not in (None, "null") and kit["light"] not in (None, "null")
            assert kit["prix_estime_eur_ht"] <= 1000


@pytest.mark.parametrize("function,arguments", [
    (search.search_venues, {"limit": 21}), (search.search_caterers, {"limit": True}),
    (search.search_providers, {"radius_km": 20}), (search.search_venues, {"city": "Namur", "radius_km": -1}),
    (search.search_venues, {"max_price": float("nan")}), (search.search_venues, {"min_capacity": True}),
    (search.search_venues, {"space": "both"}), (search.search_venues, {"accessible": "yes"}),
    (search.search_caterers, {"attendance": -1}), (search.search_caterers, {"dietary": "vegan"}),
    (search.search_providers, {"services": ["artist"]}), (search.search_providers, {"max_kit_price": float("inf")}),
])
def test_invalid_search_parameters(function, arguments):
    with pytest.raises(ValueError):
        function(**arguments)


@pytest.mark.parametrize("function,filename", [(search.search_venues, "venues.json"),
                                              (search.search_caterers, "caterers.json"),
                                              (search.search_providers, "providers.json")])
def test_structured_shortlists_are_bounded_and_smaller_than_catalogues(function, filename):
    before = data_tools._load_dataset(filename)
    result = function(limit=15)
    encoded = json.dumps(result, ensure_ascii=False)
    assert len(encoded) < 24000
    assert len(encoded) < .30 * len(json.dumps(before, ensure_ascii=False))
    assert len(result["candidates"]) == 15
    assert json.loads(encoded) == result
    assert data_tools._load_dataset(filename) == before
