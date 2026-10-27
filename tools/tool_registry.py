"""Role-specific callable/schema pairs for the existing Agent constructor.

No agents, files, or network clients are created here. Registry calls return
independent schema copies. Search schemas describe price bases; single-record tools retain full source details.
"""

from copy import deepcopy

from model.llm import tool_schema
from tools import budget_tools, data_tools, schedule_tools, search_tools


_SEARCH = {
    "city": {"type": "string", "description": "Locality or postal code in cities.json; accents and aliases supported."},
    "radius_km": {"type": "number", "minimum": 0, "description": "Straight-line radius, defaults to 30 km with city. Never road travel."},
    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 15},
}
_MONEY = {"anyOf": [{"type": "number", "minimum": 0}, {"type": "string"}],
          "description": "Non-negative EUR amount or decimal string on a consistent tax basis. Rounded to cents using ROUND_HALF_UP."}
_ACTIVITIES = {
    "type": "array",
    "description": (
        "Activities with name/start/end and optional exclusive resources. Use ISO date-time strings, "
        "all with explicit UTC offsets (or Z), or all without offsets in one local timezone. "
        "Resource IDs are exact and case-sensitive; no resources are inferred."
    ),
    "items": {
        "type": "object", "required": ["name", "start", "end"],
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "start": {"type": "string", "description": "Start, e.g. 2026-10-26T18:00:00+01:00."},
            "end": {"type": "string", "description": "End strictly after start; use the next date for an overnight activity."},
            "resources": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True,
                          "description": "Exclusive resources needed throughout this activity, e.g. a room or equipment ID."},
        },
    },
}


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    schema = tool_schema(name, description, deepcopy(properties), required=required)
    schema["function"]["parameters"]["additionalProperties"] = False
    return schema


GET_VENUE_SCHEMA = _schema(
    "get_venue", "Retrieve one complete venue by its exact source ID.",
    {"venue_id": {"type": "string", "minLength": 1, "description": "Exact venue ID, e.g. salle_001."}}, ["venue_id"],
)
GET_PROVIDER_SCHEMA = _schema(
    "get_provider", "Retrieve one technical provider and its kits by exact string ID, preserving leading zeros.",
    {"provider_id": {"type": "string", "minLength": 1, "description": "Exact provider ID, e.g. 001, not the number 1."}}, ["provider_id"],
)
GET_CATERER_SCHEMA = _schema(
    "get_caterer", "Retrieve one caterer and all its formulas by exact source ID.",
    {"caterer_id": {"type": "string", "minLength": 1, "description": "Exact caterer ID, e.g. traiteur_001."}}, ["caterer_id"],
)
GET_ITEM_SCHEMA = _schema(
    "get_item", "Resolve a saved catalogue ID. An option_id selects one technical kit or catering formula, retaining shared terms. No searches or recommendations.",
    {"category": {"type": "string", "enum": ["venue", "provider", "technical", "caterer", "catering"]},
     "item_id": {"type": "string"}, "option_id": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}},
    ["category", "item_id"],
)
SEARCH_VENUES_SCHEMA = _schema(
    "search_venues", "Filter local venues by distance, capacity, TTC rental plus cleaning. Returns at most 20 compact candidates, sorted by distance then price; fetch chosen IDs for details.",
    _SEARCH | {
        "min_capacity": {"type": "integer", "minimum": 0}, "max_price": {"type": "number", "minimum": 0},
        "space": {"type": "string", "enum": ["indoor", "outdoor"], "default": "indoor"},
        "configuration": {"type": "string", "enum": ["seated", "standing"], "default": "seated"},
        "rental": {"type": "string", "enum": ["day", "evening"], "default": "day"},
        "event_type": {"type": "string", "description": "French source event type, e.g. mariage, conference."},
        "accessible": {"type": "boolean", "default": False},
    }, [],
)
SEARCH_CATERERS_SCHEMA = _schema(
    "search_caterers", "Filter catering formulas by location, attendance, service, diets and TTC per-person ceiling (extras excluded). Returns at most 20 compact caterers.",
    _SEARCH | {
        "attendance": {"type": "integer", "minimum": 0},
        "max_price_per_person": {"type": "number", "minimum": 0},
        "service_type": {"type": "string", "description": "cocktail, buffet, service_a_table"},
        "full_meal": {"type": "boolean", "default": False},
        "dietary": {"type": "array", "items": {"type": "string"}, "description": "Required source flags such as vegetarien, vegan, sans_gluten, halal. Allergy management is not an absence-of-traces guarantee."},
    }, [],
)
SEARCH_PROVIDERS_SCHEMA = _schema(
    "search_providers", "Filter technical suppliers by location, HT kit price (tax/travel excluded), and equipment presence. Returns at most 20 compact suppliers; equipment levels are not quality ratings.",
    _SEARCH | {
        "max_kit_price": {"type": "number", "minimum": 0},
        "services": {"type": "array", "items": {"type": "string", "enum": ["son", "light", "video"]}},
    }, [],
)
CALCULATE_TOTAL_SCHEMA = _schema(
    "calculate_total", "Calculate the sum of full line prices in EUR. Each price is rounded to cents first; outputs are exact two-decimal strings. No quantities, taxes, or fees are inferred.",
    {"items": {"type": "array", "items": {
        "type": "object", "properties": {"name": {"type": "string", "minLength": 1}, "price": _MONEY},
        "required": ["name", "price"], "additionalProperties": False,
    }}}, ["items"],
)
REMAINING_BUDGET_SCHEMA = _schema(
    "remaining_budget", "Calculate remaining EUR budget and whether expenses exceed it. Monetary outputs are two-decimal strings; use a consistent tax basis.",
    {"total_budget": _MONEY, "expenses": _MONEY}, ["total_budget", "expenses"],
)
VALIDATE_BUDGET_SCHEMA = _schema(
    "validate_budget", "Validate caller-proposed EUR allocations and report their total and remaining amount, without selecting categories or adding charges. Amounts are returned as two-decimal strings.",
    {"total_budget": _MONEY, "allocations": {"type": "object", "additionalProperties": _MONEY}},
    ["total_budget", "allocations"],
)
VALIDATE_SCHEDULE_SCHEMA = _schema(
    "validate_schedule", "Validate names, datetimes, durations, required fields, and timezone consistency. This validates structure; use detect_conflicts for resource overlaps.",
    {"activities": _ACTIVITIES}, ["activities"],
)
DETECT_CONFLICTS_SCHEMA = _schema(
    "detect_conflicts", "Detect overlapping activities sharing an explicit exclusive resource. Adjacent intervals and simultaneous activities on different resources do not conflict.",
    {"activities": _ACTIVITIES}, ["activities"],
)
SORT_ACTIVITIES_SCHEMA = _schema(
    "sort_activities", "Sort valid activities chronologically, preserving all original fields and tie order. Aware datetimes are compared in UTC.",
    {"activities": _ACTIVITIES}, ["activities"],
)


def _group(*pairs) -> list[tuple]:
    return [(function, deepcopy(schema)) for function, schema in pairs]


def get_venue_tools() -> list[tuple]:
    """Venue retrieval plus dataset context; no provider selection or scoring."""
    return _group((search_tools.search_venues, SEARCH_VENUES_SCHEMA), (data_tools.get_venue, GET_VENUE_SCHEMA))


def get_provider_tools() -> list[tuple]:
    """Technical provider and kit retrieval plus dataset context."""
    return _group((search_tools.search_providers, SEARCH_PROVIDERS_SCHEMA), (data_tools.get_provider, GET_PROVIDER_SCHEMA))


def get_caterer_tools() -> list[tuple]:
    """Caterer/formula retrieval for a future catering specialist."""
    return _group((search_tools.search_caterers, SEARCH_CATERERS_SCHEMA), (data_tools.get_caterer, GET_CATERER_SCHEMA))


def get_budget_tools() -> list[tuple]:
    """Arithmetic checks only; allocation decisions remain with the agent."""
    return _group((budget_tools.calculate_total, CALCULATE_TOTAL_SCHEMA),
                  (budget_tools.remaining_budget, REMAINING_BUDGET_SCHEMA),
                  (budget_tools.validate_budget, VALIDATE_BUDGET_SCHEMA))


def get_schedule_tools() -> list[tuple]:
    """Validation, exclusive-resource conflict detection, and chronological sorting."""
    return _group((schedule_tools.validate_schedule, VALIDATE_SCHEDULE_SCHEMA),
                  (schedule_tools.detect_conflicts, DETECT_CONFLICTS_SCHEMA),
                  (schedule_tools.sort_activities, SORT_ACTIVITIES_SCHEMA))


def get_item_tools() -> list[tuple]:
    """Resolve selections for budget, schedule and writing without catalogue searches."""
    return _group((data_tools.get_item, GET_ITEM_SCHEMA))
