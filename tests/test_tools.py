import copy
import inspect
import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from model import agent as agent_module, trace
from model.agent import Agent
from model.agents import tools
from model.event_orchestrator import create_orchestrator
from model.llm import tool_schema


CATALOGS = [
    (tools.get_caterers, "caterers.json", "traiteurs", 100),
    (tools.get_providers, "providers.json", "prestataires", 40),
    (tools.get_venues, "venues.json", "salles", 150),
]


def read_rows(filename, key):
    return json.loads((tools.DATA_DIR / filename).read_text(encoding="utf-8"))[key]


@pytest.mark.parametrize("function,filename,key,count", CATALOGS)
def test_real_catalog_fallback_and_preservation(function, filename, key, count):
    original_bytes = (tools.DATA_DIR / filename).read_bytes()
    rows = read_rows(filename, key)
    assert len(rows) == count
    for filters in ({}, {"limit": 1}, {"city": "   ", "ids": []},
                    {"city": None, "limit": None}):
        assert function(**filters) == rows
    assert function(city="Ville inexistante", limit=1)["code"] == "geography_unavailable"
    assert function(ids=[rows[0]["id"]]) == [rows[0]]
    assert (tools.DATA_DIR / filename).read_bytes() == original_bytes


@pytest.mark.parametrize("function,filename,key,count", CATALOGS)
@pytest.mark.parametrize("bad_filters", [
    {"city": []}, {"city": 123}, {"limit": 0}, {"limit": -1}, {"limit": True},
    {"limit": 1.5}, {"ids": "001"}, {"ids": [1]}, {"max_budget": "100"},
    {"max_budget": -1}, {"max_budget": True}, {"max_budget": float("nan")},
    {"max_budget": float("inf")},
])
def test_invalid_filters_return_full_catalog(function, filename, key, count, bad_filters, caplog):
    assert function(**bad_filters) == read_rows(filename, key)
    assert "catalogue complet" in caplog.text


@pytest.mark.parametrize("function,filename,key,count", CATALOGS)
@pytest.mark.parametrize("content", [None, "{broken", "[]", "{}", '{"wrong": []}'])
def test_missing_corrupt_or_wrong_root(function, filename, key, count, content, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "DATA_DIR", tmp_path)
    if content is not None:
        (tmp_path / filename).write_text(content, encoding="utf-8")
    result = function(city="Namur")
    assert result["error"] and result["code"] in ("invalid_catalog", "catalog_read_error")


@pytest.mark.parametrize("function,filename,key,count", CATALOGS)
def test_invalid_rows_and_nested_filter_failure(function, filename, key, count, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(tools, "DATA_DIR", tmp_path)
    path = tmp_path / filename
    path.write_text(json.dumps({key: [123]}), encoding="utf-8")
    assert function()["code"] == "invalid_catalog"
    rows = [{"id": "original", "lieux": 3}]
    path.write_text(json.dumps({key: rows}), encoding="utf-8")
    assert function(city="Namur", limit=1) == rows
    # An unexpected filtering error must also preserve all readable dictionaries.
    monkeypatch.setattr(tools, "_common_match", Mock(side_effect=RuntimeError("filter failure")))
    assert function(ids=["original"]) == rows
    assert "filter failure" in caplog.text


def test_search_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert len(tools.get_venues()) == 150


@pytest.mark.parametrize("function,filename,key,count", CATALOGS)
def test_catalog_permission_error(function, filename, key, count, monkeypatch):
    monkeypatch.setattr(type(tools.DATA_DIR), "open", Mock(side_effect=PermissionError("denied")))
    assert function()["code"] == "catalog_read_error"


def test_caterer_filters_on_real_catalog():
    rows = read_rows("caterers.json", "traiteurs")
    expected = [r for r in rows if "Namur" in r["zones_intervention"]
                and "mariage" in r["types_evenements_adaptes"]
                and r["options_alimentaires"]["vegan"]
                and any(f["repas_complet"] and f["type_service"] == "buffet"
                        and f["minimum_personnes"] <= 100 <= f["maximum_personnes"]
                        and f["prix_par_personne_ttc"] <= 50
                        and 100 * f["prix_par_personne_ttc"] + r["frais_supplementaires"]["livraison_forfait_ttc"] <= 5000
                        for f in r["formules"])]
    assert expected and len(expected) < len(rows)
    filters = dict(province=" namur ", event_type="Mariage", guests=100, full_meal=True,
                   service_type="buffet", dietary_options=["vegan"],
                   max_price_per_person=50, max_budget=5000)
    assert tools.get_caterers(**filters, limit=None) == expected
    assert tools.get_caterers(**filters, limit=1) == expected[:1]
    assert tools.get_caterers(max_budget=5000) == rows
    assert tools.get_caterers(guests=False) == rows
    assert tools.get_caterers(full_meal="true") == rows


def test_caterer_criteria_cannot_match_different_formulas(tmp_path, monkeypatch):
    base = read_rows("caterers.json", "traiteurs")[0]
    split = copy.deepcopy(base)
    split["id"] = "split"
    split["formules"] = [dict(base["formules"][0], repas_complet=False, prix_par_personne_ttc=10),
                         dict(base["formules"][1], repas_complet=True, prix_par_personne_ttc=100)]
    match = copy.deepcopy(split)
    match["id"] = "match"
    match["formules"][1]["prix_par_personne_ttc"] = 20
    monkeypatch.setattr(tools, "DATA_DIR", tmp_path)
    (tmp_path / "caterers.json").write_text(json.dumps({"traiteurs": [split, match]}), encoding="utf-8")
    assert tools.get_caterers(full_meal=True, max_price_per_person=30) == [match]


def test_providers_budget_levels_and_absent_video():
    rows = read_rows("providers.json", "prestataires")
    expected = [r for r in rows if any(k["prix_estime_eur_ht"] <= 3000
                and k["son"] in ("bien", "trés bien", "exelent", "parfait")
                and k["video"] == "null" for k in r["kits"])]
    assert expected and len(expected) < len(rows)
    assert tools.get_providers(max_budget=3000, sound_level="bien", video_level="null", limit=None) == expected
    assert tools.get_providers(max_budget=3000, sound_level="bien", video_level="null", limit=1) == expected[:1]
    assert tools.get_providers(sound_level="invalide") == rows
    assert tools.get_providers(ids=["001"])[0]["id"] == "001"


def test_provider_criteria_cannot_match_different_kits(tmp_path, monkeypatch):
    base = read_rows("providers.json", "prestataires")[0]
    split = copy.deepcopy(base)
    split["kits"] = [dict(base["kits"][0], prix_estime_eur_ht=10, son="simple"),
                     dict(base["kits"][1], prix_estime_eur_ht=1000, son="parfait")]
    match = copy.deepcopy(split)
    match["id"] = "002"
    match["kits"][1]["prix_estime_eur_ht"] = 50
    monkeypatch.setattr(tools, "DATA_DIR", tmp_path)
    (tmp_path / "providers.json").write_text(json.dumps({"prestataires": [split, match]}), encoding="utf-8")
    assert tools.get_providers(max_budget=100, sound_level="bien") == [match]


def test_provider_city_matches_a_place_not_a_substring():
    # 'As' is a commune, and must not match 'Wasseiges'.
    assert [r["id"] for r in tools.get_providers(city="As", limit=None)] == ["006"]


def test_provider_category_and_missing_province():
    rows = read_rows("providers.json", "prestataires")
    target = next(r for r in rows if r.get("categorie_taille") and r.get("province") and "/" not in r["province"])
    expected = [r for r in rows if r.get("categorie_taille") == target["categorie_taille"]
                and r.get("province") == target["province"]]
    assert expected and len(expected) < len(rows)
    assert any("province" not in r for r in rows)
    assert tools.get_providers(category=target["categorie_taille"], province=target["province"], limit=None) == expected


def test_venue_capacity_budget_and_event_filters():
    rows = read_rows("venues.json", "salles")
    expected = [r for r in rows if "mariage" in r["types_evenements_adaptes"]
                and "mariage" not in r["types_evenements_interdits"]
                and r["capacite"]["interieur"]["personnes_assises"] >= 100
                and r["capacite"]["maximum_personnes_sur_site"] >= 100
                and r["prix"]["location_soiree_ttc"] + r["prix"]["nettoyage_obligatoire_ttc"] <= 4000]
    assert expected and len(expected) < len(rows)
    filters = dict(event_type="mariage", guests=100, seated=True, space_type="intérieur",
                   max_budget=4000, rental_period="soirée")
    assert tools.get_venues(**filters, limit=None) == expected
    assert tools.get_venues(**filters, limit=1) == expected[:1]
    assert tools.get_venues(seated=True, rental_period="soiree", limit=1) == rows


def test_venue_no_capacity_summing_or_ignored_cleaning(tmp_path, monkeypatch):
    base = read_rows("venues.json", "salles")[0]
    too_small = copy.deepcopy(base)
    fits = copy.deepcopy(base)
    fits["id"] = "fits"
    fits["capacite"]["interieur"]["personnes_assises"] = 150
    too_expensive = copy.deepcopy(fits)
    too_expensive["id"] = "expensive"
    too_expensive["prix"]["nettoyage_obligatoire_ttc"] = 1000
    rows = [too_small, fits, too_expensive]
    monkeypatch.setattr(tools, "DATA_DIR", tmp_path)
    (tmp_path / "venues.json").write_text(json.dumps({"salles": rows}), encoding="utf-8")
    # First venue has 80 indoor + 112 outdoor seats, but cannot seat 150 in one space.
    assert tools.get_venues(guests=150, seated=True, max_budget=2100) == [fits]


def test_venue_exclusivity_and_accent_matching():
    rows = read_rows("venues.json", "salles")
    expected = [r for r in rows
                if (not r["contraintes"]["traiteur_impose"] or "traiteur_001" in r["contraintes"]["traiteurs_autorises_ids"])
                and (not r["contraintes"]["prestataire_technique_impose"] or "002" in r["contraintes"]["prestataires_techniques_autorises_ids"])]
    assert expected and len(expected) < len(rows)
    assert tools.get_venues(caterer_id="traiteur_001", provider_id="002", limit=None) == expected
    liege = tools.get_venues(city=" LIEGE ", limit=None)
    assert 0 < len(liege) < len(rows)
    assert all(r["lieux"]["commune"] == "Liège" for r in liege)


@pytest.mark.parametrize("answers,approved", [(["oui"], True), ([" OUI "], True), (["non"], False),
                                            (["maybe", "", "yes"], True), (["?", "n"], False)])
def test_ask_user(answers, approved, monkeypatch):
    console = Mock()
    console.input.side_effect = answers
    monkeypatch.setattr(trace, "console", console)
    message = "[bold]Contenu complet[/bold]\n" + "x" * 500
    assert tools.ask_user(message) == {"approved": approved}
    console.print.assert_any_call(message, markup=False, highlight=False)


@pytest.mark.parametrize("exception", [EOFError(), KeyboardInterrupt(), OSError("no stdin")])
def test_ask_user_interrupted(exception, monkeypatch):
    monkeypatch.setattr(trace.console, "input", Mock(side_effect=exception))
    assert tools.ask_user("Valider ?") == {"approved": False}


@pytest.mark.parametrize("message", [None, 123, "", "  "])
def test_ask_user_invalid_message(message):
    assert tools.ask_user(message) == {"approved": False}


@pytest.mark.parametrize("day,reference,expected", [
    ("monday", "2026-11-16", "2026-11-23"),
    (" MONDAY ", "2026-11-17", "2026-11-23"),
    ("wednesday", "2026-11-17", "2026-11-18"),
    ("friday", "2026-12-31", "2027-01-01"),
    ("thursday", "2024-02-28", "2024-02-29"),
])
def test_next_day(day, reference, expected):
    assert tools.get_next_day(day, reference) == expected


@pytest.mark.parametrize("day,reference", [(None, None), ([], None), ("invalid", None),
    ("monday", ""), ("monday", "2026-02-30"), ("monday", "20261117"),
    ("monday", 123), ("monday", "9999-12-31")])
def test_invalid_next_day(day, reference):
    assert tools.get_next_day(day, reference)["error"]


@pytest.fixture
def fixed_today(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 17)
    monkeypatch.setattr(tools, "date", FixedDate)
    return FixedDate.today()


def test_next_day_defaults_to_today(fixed_today):
    assert tools.get_next_day("tuesday") == "2026-11-24"
    assert tools.get_next_day("tuesday", None) == "2026-11-24"


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


GEO = {"results": [{"name": "Namur", "country": "Belgique", "latitude": 50.46, "longitude": 4.86}]}


def weather_payload(day):
    return {"daily": {"time": [day], **{field: [i] for i, field in enumerate(tools.WEATHER_FIELDS.values())}}}


@pytest.mark.parametrize("offset", [0, 1, 7, 15])
def test_weather_available_dates(offset, fixed_today, monkeypatch):
    day = (fixed_today + timedelta(days=offset)).isoformat()
    get = Mock(side_effect=[response(GEO), response(weather_payload(day))])
    monkeypatch.setattr(tools.requests, "get", get)
    result = tools.get_meteo(day, " Namur ")
    assert result == {"city": "Namur", "country": "Belgique", "date": day,
                      **dict(zip(tools.WEATHER_FIELDS, range(6))), "source": "Open-Meteo"}
    assert get.call_count == 2
    assert get.call_args.kwargs["params"]["start_date"] == day
    assert get.call_args.kwargs["params"]["end_date"] == day
    assert all(call.kwargs["timeout"] == 10 for call in get.call_args_list)


@pytest.mark.parametrize("day,city,code", [
    ("2026-11-16", "Namur", "forecast_unavailable"), ("2026-12-03", "Namur", "forecast_unavailable"),
    ("2026-02-30", "Namur", "invalid_date"), ("20261117", "Namur", "invalid_date"),
    (None, "Namur", "invalid_date"), (123, "Namur", "invalid_date"),
    ("2026-11-17", "", "invalid_city"), ("2026-11-17", None, "invalid_city"),
])
def test_weather_invalid_inputs_without_network(day, city, code, fixed_today):
    assert tools.get_meteo(day, city)["code"] == code


@pytest.mark.parametrize("payload,code", [({}, "unknown_city"), ([], "invalid_api_response"),
    ({"results": None}, "invalid_api_response"), ({"results": [{}]}, "invalid_api_response"),
    ({"error": True, "reason": "bad request"}, "api_error")])
def test_weather_invalid_geocoding(payload, code, fixed_today, monkeypatch):
    get = Mock(return_value=response(payload))
    monkeypatch.setattr(tools.requests, "get", get)
    assert tools.get_meteo("2026-11-18", "Namur")["code"] == code
    assert get.call_count == 1


@pytest.mark.parametrize("stage", [0, 1])
@pytest.mark.parametrize("exception", [requests.Timeout("timeout"), requests.HTTPError("503"),
                                        requests.ConnectionError("offline"), ValueError("invalid JSON")])
def test_weather_api_errors(stage, exception, fixed_today, monkeypatch):
    broken = response(None)
    if isinstance(exception, ValueError):
        broken.json.side_effect = exception
    else:
        broken.raise_for_status.side_effect = exception
    get = Mock(side_effect=[response(GEO), broken] if stage else [broken])
    monkeypatch.setattr(tools.requests, "get", get)
    result = tools.get_meteo("2026-11-18", "Namur")
    assert result["error"] and result["code"] in ("api_error", "invalid_api_response")


@pytest.mark.parametrize("problem", ["no_daily", "wrong_day", "missing", "empty", "null", "wrong_type", "nan"])
def test_weather_unavailable_fields(problem, fixed_today, monkeypatch):
    payload = weather_payload("2026-11-18")
    field = "temperature_2m_min"
    if problem == "no_daily":
        payload = {}
    elif problem == "wrong_day":
        payload["daily"]["time"] = ["2026-11-19"]
    elif problem == "missing":
        del payload["daily"][field]
    else:
        payload["daily"][field] = {"empty": [], "null": [None], "wrong_type": "12", "nan": [float("nan")]}[problem]
    monkeypatch.setattr(tools.requests, "get", Mock(side_effect=[response(GEO), response(payload)]))
    assert tools.get_meteo("2026-11-18", "Namur")["code"] == "forecast_unavailable"


def test_weather_selects_requested_day_not_first_index(fixed_today, monkeypatch):
    payload = weather_payload("2026-11-18")
    for values in payload["daily"].values():
        values.insert(0, "2026-11-17" if isinstance(values[0], str) else -100)
    monkeypatch.setattr(tools.requests, "get", Mock(side_effect=[response(GEO), response(payload)]))
    assert tools.get_meteo("2026-11-18", "Namur")["temperature_min"] == 0


PAIRS = [(tools.get_caterers, tools.SCHEMA_CATERERS), (tools.get_providers, tools.SCHEMA_PROVIDERS),
         (tools.get_venues, tools.SCHEMA_VENUES), (tools.ask_user, tools.SCHEMA_ASK_USER),
         (tools.get_next_day, tools.SCHEMA_NEXT_DAY), (tools.get_meteo, tools.SCHEMA_METEO)]


@pytest.mark.parametrize("function,schema", PAIRS)
def test_schemas_match_signatures(function, schema):
    params = inspect.signature(function).parameters
    schema_params = schema["function"]["parameters"]
    assert schema["function"]["name"] == function.__name__
    assert set(schema_params["properties"]) == set(params)
    assert set(schema_params["required"]) == {n for n, p in params.items() if p.default is inspect.Parameter.empty}
    assert schema_params["additionalProperties"] is False
    for name, param in params.items():
        if param.default is None:
            assert "null" in schema_params["properties"][name]["type"]


def test_schema_required_default_and_explicit_empty():
    properties = {"a": {"type": "string"}}
    assert tool_schema("x", "x", properties)["function"]["parameters"]["required"] == ["a"]
    assert tool_schema("x", "x", properties, requis=[])["function"]["parameters"]["required"] == []


def call(name, arguments):
    return SimpleNamespace(id="call_1", function=SimpleNamespace(name=name, arguments=arguments))


@pytest.mark.parametrize("arguments", ["{broken", "[]", "null", "123", [1], []])
def test_agent_rejects_malformed_arguments(arguments):
    agent = Agent("test", "test", [(tools.get_venues, tools.SCHEMA_VENUES)])
    assert json.loads(agent.execute_tool(call("get_venues", arguments)))["code"] == "invalid_arguments"


def test_agent_tool_execution_and_errors():
    agent = Agent("test", "test", [(tools.get_venues, tools.SCHEMA_VENUES)])
    result = json.loads(agent.execute_tool(call("get_venues", '{"city": "Namur"}')))
    assert result and all(r["lieux"]["commune"] == "Namur" for r in result)
    assert json.loads(agent.execute_tool(call("get_venues", '{"unexpected": 1}')))["code"] == "tool_execution_error"
    assert json.loads(agent.execute_tool(call("missing", '{}')))["code"] == "unknown_tool"


def test_orchestrator_creation_and_mocked_agent_run(monkeypatch):
    supervisor = create_orchestrator()
    assert set(supervisor.fonctions) == {"budgetary_agent", "writer_agent", "planning_agent", "decision_making_agent"}
    agent = Agent("test", "test", [(tools.get_venues, tools.SCHEMA_VENUES)])
    chat = Mock(side_effect=[SimpleNamespace(content=None, tool_calls=[call("get_venues", '{"city":"Namur"}')]),
                             SimpleNamespace(content="Terminé", tool_calls=None)])
    monkeypatch.setattr(agent_module, "chat", chat)
    assert agent.run("Salle à Namur") == "Terminé"
    messages = chat.call_args.args[0]
    assert messages[-1]["role"] == "tool"
    assert json.loads(messages[-1]["content"])[0]["lieux"]["commune"] == "Namur"


def test_trace_can_display_long_tool_results():
    assert trace.reduce_text("x" * 500) == "x" * 400 + "..."
