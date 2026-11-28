"""Recherche de proximité et propagation du lieu réel, sans réseau ni LLM."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from model import agent as agent_module, geography
from model.agents import create_all_agents, tools
from model.agents.search import search_candidates, load_catalogs
from model.event_state import EventRun


@pytest.fixture
def overpass(monkeypatch):
    geography._rank_cities.cache_clear()
    monkeypatch.setenv("OVERPASS_URL", "https://overpass.example/api")
    monkeypatch.setenv("OVERPASS_KEY", "test-key")
    response = Mock()
    response.json.return_value = {"elements": [
        {"lat": 50.46, "lon": 4.87, "tags": {"name": "Namur"}},
        {"lat": 50.26, "lon": 4.91, "tags": {"name": "Dinant"}},
        {"lat": 50.63, "lon": 5.57, "tags": {"name": "Liège"}},
    ]}
    post = Mock(return_value=response)
    monkeypatch.setattr(geography.requests, "post", post)
    monkeypatch.setattr(tools, "select_filtered", geography.select_filtered)
    yield post
    geography._rank_cities.cache_clear()


def test_wip_distance_order_endpoint_key_and_cache(overpass):
    assert geography.distance(50.46, 4.87, 50.46, 4.87) == 0
    for _ in range(2):
        assert geography.select_filtered("venues", " Namur ", cities=["Liège", "Dinant"]) == ["Dinant", "Liège"]
    overpass.assert_called_once()
    assert overpass.call_args.args == ("https://overpass.example/api/interpreter",)
    assert overpass.call_args.kwargs["headers"] == {"X-API-Key": "test-key"}
    assert '"ISO3166-1"="BE"' in overpass.call_args.kwargs["data"]["data"]


def test_overpass_errors_are_explicit_and_do_not_expose_url(overpass):
    overpass.side_effect = requests.Timeout("https://secret.example/private-key")
    with pytest.raises(RuntimeError, match="Timeout") as error:
        geography.select_filtered("venues", "Namur", cities=["Dinant"])
    assert "secret" not in str(error.value)
    result = search_candidates("venue", {"city": "Namur", "ids": ["salle_109"]})
    assert result["status"] == "error" and result["candidates"] == []


def test_unknown_origin_and_empty_geocoding_are_errors(overpass):
    with pytest.raises(ValueError, match="départ introuvable"):
        geography.select_filtered("venues", "Ville inconnue", cities=["Dinant"])
    overpass.return_value.json.return_value = {"elements": []}
    with pytest.raises(ValueError):
        geography.select_filtered("venues", "Namur", cities=["Dinant"])


def test_accents_and_provider_multiple_communes(overpass):
    assert geography.select_filtered("providers", "liege", cities=["Namur", "Dinant"]) == ["Namur", "Dinant"]
    rows = [{"id": "001", "lieux": "Dinant / Namur (Dinant)"},
            {"id": "002", "lieux": "Liège"}]
    selected, location = tools.nearest_candidates("providers.json", rows, "namur")
    assert selected == [rows[0]] and not location["expanded"]


def test_missing_overpass_config_and_malformed_response(overpass, monkeypatch):
    monkeypatch.delenv("OVERPASS_URL")
    with pytest.raises(ValueError, match="OVERPASS_URL"):
        geography.select_filtered("venues", "Namur", cities=["Dinant"])
    monkeypatch.setenv("OVERPASS_URL", "https://overpass.example/api/interpreter")
    overpass.return_value.json.return_value = {"elements": [None]}
    with pytest.raises(RuntimeError, match="invalide"):
        geography.select_filtered("venues", "Namur", cities=["Dinant"])


def test_nearest_compatible_city_before_limit_and_without_relaxing_filters(overpass, monkeypatch):
    catalogs = load_catalogs()
    base = copy.deepcopy(catalogs["venue"][0])
    rows = []
    for index, city in enumerate(["Liège", "Namur", "Dinant"]):
        row = copy.deepcopy(base)
        row.update(id=str(index), lieux={**row["lieux"], "commune": city})
        row["capacite"] = 10 if city == "Namur" else 100
        rows.append(row)
    catalogs["venue"] = rows
    filters = {"city": "Namur", "guests": 80, "limit": 1}
    result = search_candidates("venue", filters, catalogs=catalogs)
    assert [row["id"] for row in result["candidates"]] == ["2"]
    assert result["location_search"]["selected_city"] == "Dinant"
    monkeypatch.setattr(tools, "_read_catalog", lambda *_: rows)
    assert tools.get_venues(**filters) == [rows[2]]
    assert tools.get_venues(**{**filters, "guests": 1000}) == []
    assert search_candidates("venue", filters, catalogs=catalogs,
                             context={"location_scope": "city"})["status"] == "no_match"


def test_local_candidates_need_no_overpass(overpass):
    assert search_candidates("venue", {"city": "Namur"})["status"] == "matched"
    overpass.assert_not_called()


def test_namur_to_dinant_recenters_services_weather_and_documents(monkeypatch):
    run = EventRun("Soirée étudiante à Namur")
    run.remember({"city": "Namur", "guests": 120,
                  "planning": {"start": "2027-12-05T22:00", "end": "2027-12-06T04:00"}},
                 ["venue", "catering", "provider"])
    _, _, _, decision = create_all_agents()
    captured = []

    def choose(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        captured.append(payload)
        row = payload["search"]["candidates"][0]
        selected = {"id": row["id"]}
        if payload["task"] == "catering":
            selected["formula_id"] = row["formules"][0]["id"]
        if payload["task"] == "provider":
            selected["kit_id"] = row["kits"][0]["id"]
        return SimpleNamespace(content=json.dumps(selected), tool_calls=None)

    monkeypatch.setattr(agent_module, "chat", choose)
    assert run.delegate(decision, "Traiteur", "catering")["requires"] == "venue"
    assert run.delegate(decision, "Salle", "venue")["status"] == "success"
    assert run.selected_row("venue")["lieux"]["commune"] == "Dinant"
    assert run.event_data["requirements"]["city"] == "Namur"
    for category in ("catering", "provider"):
        run.event_data["requirements"][category + "_constraints"] = {"city": "Namur"}
        assert run.delegate(decision, category, category)["status"] == "success"
        assert captured[-1]["filters"]["city"] == "Dinant"
        assert captured[-1]["search"]["location_search"]["requested_city"] == "Dinant"
    weather = Mock(return_value={"error": "test", "code": "forecast_unavailable"})
    monkeypatch.setattr(tools, "get_meteo", weather)
    run.observe_weather("2027-12-05", "Namur")
    run.observe_weather("2027-12-05", "Namur")
    weather.assert_called_once_with(day="2027-12-05", city="Dinant")
    assert run.get_planning_context()["requirements"]["city"] == "Dinant"
    assert run.get_writer_context()["requirements"]["city"] == "Dinant"
    assert "Dinant" in run.summary("")

    new_venue = next(row for row in run.catalogs["venue"] if row["lieux"]["commune"] == "Namur")
    run.record("venue", {"status": "success", "result": {"id": new_venue["id"]}})
    assert run.event_data["results"]["catering"] is None
    assert run.event_data["results"]["providers"] == []
    assert run.context("provider")["filters"]["city"] == "Namur"
