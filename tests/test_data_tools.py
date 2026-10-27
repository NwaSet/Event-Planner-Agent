"""Read-only dataset contracts using actual data and isolated in-memory files."""

from copy import deepcopy
import hashlib
from io import StringIO
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import data_tools as data


@pytest.fixture
def files(monkeypatch):
    directory = (Path(__file__).resolve().parent / "virtual_data").resolve()
    contents = {
        "venues.json": {"meta": {"usage": "Fixture"}, "salles": [
            {"id": "z", "nom": "Salle à Liège", "description": "Preserve all constraints", "price": 90},
            {"id": "a", "nom": "Another venue", "price": 10},
        ]},
        "providers.json": {"meta": {}, "referentiels": {"unknown": None, "absent": "null"}, "prestataires": [
            {"id": "002", "kits": [{"id": 1, "description": "Complete kit", "price": 90}]},
            {"id": "001", "kits": [{"id": 1, "description": "Other kit", "price": 10}]},
        ]},
        "caterers.json": {"meta": {}, "traiteurs": [{"id": "traiteur_001", "formules": [{"id": "formule_01"}]}]},
    }
    original = Path.open
    opened = []

    def open_fixture(path, mode="r", **kwargs):
        if path.parent != directory:
            return original(path, mode, **kwargs)
        opened.append((path.name, mode, kwargs))
        if path.name not in contents:
            raise FileNotFoundError
        value = contents[path.name]
        if isinstance(value, Exception):
            raise value
        return StringIO(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))

    monkeypatch.setattr(data, "_DATA_DIR", directory)
    monkeypatch.setattr(Path, "open", open_fixture)
    return contents, opened


def test_actual_datasets_counts_metadata_and_read_only():
    paths = sorted(data._DATA_DIR.glob("*.json"))
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    info = {item["filename"]: item for item in data.list_datasets()}
    assert {name: item["record_count"] for name, item in info.items()} == {
        "venues.json": 150, "providers.json": 40, "caterers.json": 100,
    }
    assert info["venues.json"]["metadata"]["fichier_traiteurs"] == "caterers.json"
    assert "null_json" in info["providers.json"]["reference_data"]["referentiels"]
    for filename, collection, get_page, get_one in [
        ("venues.json", "salles", data.get_venues, data.get_venue),
        ("providers.json", "prestataires", data.get_providers, data.get_provider),
        ("caterers.json", "traiteurs", data.get_caterers, data.get_caterer),
    ]:
        source = json.loads((data._DATA_DIR / filename).read_text(encoding="utf-8"))[collection]
        retrieved = []
        for offset in range(0, info[filename]["record_count"], 50):
            retrieved.extend(get_page(offset, 50))
        assert retrieved == source  # No ranking, omissions, or transformed business fields.
        assert get_one(source[0]["id"]) == source[0]
        json.dumps(retrieved, allow_nan=False, ensure_ascii=False)
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    assert before == after


def test_actual_relationships_are_resolvable():
    venues = data._load_dataset("venues.json")["salles"]
    providers = {item["id"] for item in data._load_dataset("providers.json")["prestataires"]}
    caterers = {item["id"] for item in data._load_dataset("caterers.json")["traiteurs"]}
    assert all(set(item["contraintes"]["prestataires_techniques_autorises_ids"]) <= providers for item in venues)
    assert all(set(item["contraintes"]["traiteurs_autorises_ids"]) <= caterers for item in venues)


def test_pagination_preserves_order_fields_and_independence(files):
    contents, opened = files
    expected = deepcopy(contents["venues.json"]["salles"])
    assert data.get_venues(limit=1) == expected[:1]
    assert data.get_venues(offset=1, limit=1) == expected[1:]
    assert data.get_venues(100, 10) == []
    returned = data.get_venue("z")
    returned["description"] = "Changed locally"
    assert data.get_venue("z") == expected[0]
    assert all(mode == "r" and kwargs["encoding"] == "utf-8" for _, mode, kwargs in opened)


def test_provider_id_zeros_nested_fields_and_original_order(files):
    assert [item["id"] for item in data.get_providers()] == ["002", "001"]
    assert data.get_provider("001")["kits"][0]["description"] == "Other kit"
    assert data.get_caterer("traiteur_001")["formules"][0]["id"] == "formule_01"
    assert data.get_caterers() == files[0]["caterers.json"]["traiteurs"]
    with pytest.raises(LookupError):
        data.get_provider("1")
    with pytest.raises(ValueError):
        data.get_provider(1)


@pytest.mark.parametrize("identifier", ["", " ", None, [], True])
def test_invalid_ids(identifier, files):
    with pytest.raises(ValueError):
        data.get_venue(identifier)


@pytest.mark.parametrize("filename", ["../.env", "..\\.env", "venues.json/../.env", "C:\\secrets.json",
                                       "/etc/passwd", "unknown.json", "venues.json:extra", None, []])
def test_unregistered_paths_rejected_before_reading(filename, monkeypatch):
    opener = Mock(side_effect=AssertionError("No file should be opened"))
    monkeypatch.setattr(Path, "open", opener)
    with pytest.raises(ValueError):
        data._load_dataset(filename)
    opener.assert_not_called()


def test_symlink_escape_rejected_without_requiring_windows_symlink_privileges(monkeypatch):
    original = Path.resolve
    directory = data._DATA_DIR.resolve()
    target = directory / "venues.json"
    outside = directory.parent / "outside" / "venues.json"
    monkeypatch.setattr(Path, "resolve", lambda path: outside if path == target else original(path))
    with pytest.raises(ValueError, match="registered location"):
        data._load_dataset("venues.json")


@pytest.mark.parametrize("offset,limit", [(-1, 10), (True, 10), (0.5, 10), (0, 0), (0, -1),
                                         (0, True), (0, 51), (0, "10")])
def test_invalid_page_arguments(offset, limit, files):
    with pytest.raises(ValueError):
        data.get_venues(offset, limit)
    assert files[1] == []


@pytest.mark.parametrize("value", ["not JSON", '{"salles": [{"id":"a","price":NaN}]}',
                                   '{"salles": [{"id":"a","price":1e999}]}',
                                   '{"salles": [], "salles": []}', [], {"wrong": []},
                                   {"salles": [None]}, {"salles": [{"id": 1}]},
                                   {"salles": [{"id": "a"}, {"id": "a"}]}, {"salles": [], "meta": None}])
def test_invalid_json_or_dataset_structure(value, files):
    files[0]["venues.json"] = value
    with pytest.raises(ValueError):
        data.get_venues()


def test_missing_and_unreadable_file_errors(files):
    del files[0]["venues.json"]
    with pytest.raises(FileNotFoundError, match="venues.json"):
        data.get_venues()
    files[0]["venues.json"] = PermissionError("private-path-details")
    with pytest.raises(OSError) as failure:
        data.get_venues()
    assert "private-path-details" not in str(failure.value)


def test_empty_and_large_dataset_counts(files):
    contents, _ = files
    contents["venues.json"]["salles"] = []
    assert data.get_venues() == []
    assert data.list_datasets()[0]["record_count"] == 0
    with pytest.raises(LookupError):
        data.get_venue("missing")
    records = [{"id": str(index), "description": f"Fixture {index}"} for index in range(123)]
    contents["venues.json"]["salles"] = records
    assert data.list_datasets()[0]["record_count"] == 123
    assert data.get_venues(0, 50) + data.get_venues(50, 50) + data.get_venues(100, 50) == records


def test_generic_item_access_resolves_exact_option_and_shared_terms():
    from tools.data_tools import get_item
    venue = get_item("venue", "salle_054")
    assert venue["id"] == "salle_054"
    provider = get_item("provider", "002", option_id=6)
    assert provider["id"] == "002" and provider["option"]["id"] == 6
    assert "kits" not in provider and "deplacement" in provider
    catering = get_item("catering", "traiteur_001", option_id="formule_02")
    assert catering["option"]["id"] == "formule_02" and "formules" not in catering
    assert "frais_supplementaires" in catering


@pytest.mark.parametrize("category,item_id,option", [("artist", "001", None), ("venue", "salle_054", 1),
                                                  ("provider", "002", "6"), ("provider", "002", True),
                                                  ("catering", "traiteur_001", "unknown")])
def test_generic_item_access_rejects_missing_or_mismatched_options(category, item_id, option):
    from tools.data_tools import get_item
    with pytest.raises((ValueError, LookupError)):
        get_item(category, item_id, option)
