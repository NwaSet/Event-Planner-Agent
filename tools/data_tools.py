"""Read the three project datasets without ranking, filtering, or altering records.

Original French field names, qualitative descriptions, nulls, and string IDs are
preserved. Pages follow file order. list_datasets() exposes actual record counts,
source metadata, relationship explanations, and provider reference definitions.
Provider IDs such as '001' retain their leading zeros. Caterer formula IDs and
provider kit IDs are local to their enclosing record, not globally unique.
"""

import json
from pathlib import Path


DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50
_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_DATASETS = {
    "venues.json": "salles",
    "providers.json": "prestataires",
    "caterers.json": "traiteurs",
}


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object keys are not supported.")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError("Non-finite JSON values are not supported.")


def _load_dataset(filename: str) -> dict:
    """Load only a known dataset, rejecting traversal and redirected file paths."""
    if not isinstance(filename, str) or filename not in _DATASETS:
        raise ValueError("Only the registered project datasets may be accessed.")
    directory = _DATA_DIR.resolve()
    path = (directory / filename).resolve()
    if path.parent != directory or path.name != filename:
        raise ValueError("Dataset paths must resolve to their registered location inside data/.")
    try:
        with path.open("r", encoding="utf-8") as source:
            data = json.load(source, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        # Also reject overflowing JSON numeric literals such as 1e999.
        json.dumps(data, allow_nan=False)
    except FileNotFoundError:
        raise FileNotFoundError(f"Required dataset {filename} was not found.") from None
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError(f"Dataset {filename} does not contain valid, unambiguous JSON.") from None
    except OSError:
        raise OSError(f"Dataset {filename} could not be read.") from None
    collection = _DATASETS[filename]
    if not isinstance(data, dict) or not isinstance(data.get(collection), list):
        raise ValueError(f"Dataset {filename} must contain a '{collection}' array.")
    if "meta" in data and not isinstance(data["meta"], dict):
        raise ValueError(f"Dataset {filename} has invalid metadata.")
    identifiers = set()
    for record in data[collection]:
        identifier = record.get("id") if isinstance(record, dict) else None
        if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
            raise ValueError(f"Dataset {filename} must contain records with unique, non-empty string IDs.")
        identifiers.add(identifier)
    return data


def _page(filename: str, offset: int, limit: int) -> list[dict]:
    if type(offset) is not int or offset < 0:
        raise ValueError("Offset must be a non-negative integer.")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"Limit must be an integer between 1 and {MAX_PAGE_SIZE}.")
    return _load_dataset(filename)[_DATASETS[filename]][offset:offset + limit]


def _record(filename: str, identifier: str) -> dict:
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("Record ID must be a non-empty string, preserving any leading zeros.")
    for record in _load_dataset(filename)[_DATASETS[filename]]:
        if record["id"] == identifier:
            return record
    raise LookupError(f"No record matches the supplied ID in {filename}.")


def get_venues(offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> list[dict]:
    """Retrieve a page of complete venue records, in their original file order."""
    return _page("venues.json", offset, limit)


def get_venue(venue_id: str) -> dict:
    """Retrieve a venue by exact ID, for example 'salle_001'."""
    return _record("venues.json", venue_id)


def get_providers(offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> list[dict]:
    """Retrieve a page of technical providers, retaining all kits and descriptions."""
    return _page("providers.json", offset, limit)


def get_provider(provider_id: str) -> dict:
    """Retrieve a technical provider by exact string ID, for example '001'."""
    return _record("providers.json", provider_id)


def get_caterers(offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> list[dict]:
    """Retrieve a page of caterers, retaining formulas, dietary options, and constraints."""
    return _page("caterers.json", offset, limit)


def get_caterer(caterer_id: str) -> dict:
    """Retrieve a caterer by exact ID, for example 'traiteur_001'."""
    return _record("caterers.json", caterer_id)


def get_item(category: str, item_id: str, option_id: str | int | None = None) -> dict:
    """Resolve a stored ID, optionally returning only its kit/formula and shared terms."""
    catalogs = {"venue": (get_venue, None), "provider": (get_provider, "kits"),
                "technical": (get_provider, "kits"), "caterer": (get_caterer, "formules"),
                "catering": (get_caterer, "formules")}
    if category not in catalogs:
        raise ValueError("No catalogue exists for this category.")
    retrieve, options = catalogs[category]
    record = retrieve(item_id)
    if option_id is None:
        return record
    if options is None or isinstance(option_id, bool):
        raise ValueError("This category/option identifier is invalid.")
    for option in record[options]:
        if type(option["id"]) is type(option_id) and option["id"] == option_id:
            return {key: value for key, value in record.items() if key != options} | {"option": option}
    raise LookupError("The option does not belong to this catalogue item.")


def list_datasets() -> list[dict]:
    """Return counts and source context so callers can traverse every record.

    Metadata describes simulated prices and relationships. Reference definitions
    distinguish JSON null (unknown) from the provider kit string 'null' (absent).
    No records or fields are synthesized to fill missing business information.
    """
    result = []
    for filename, collection in _DATASETS.items():
        data = _load_dataset(filename)
        result.append({
            "filename": filename, "collection": collection, "id_field": "id",
            "record_count": len(data[collection]), "default_page_size": DEFAULT_PAGE_SIZE,
            "max_page_size": MAX_PAGE_SIZE, "metadata": data.get("meta", {}),
            "reference_data": {key: value for key, value in data.items() if key not in {"meta", collection}},
        })
    return result
