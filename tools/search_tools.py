"""Small offline catalogue searches. Python filters; the LLM chooses and explains."""

import json
import math
import re
import unicodedata

from tools import data_tools


def _normalize(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if c.isalnum())


def _cities() -> dict:
    data = json.loads((data_tools._DATA_DIR / "cities.json").read_text(encoding="utf-8"))
    return {_normalize(name): city for city in data["cities"]
            for name in [city["name"], city["postal_code"], *city.get("aliases", [])]}


def haversine(latitude1: float, longitude1: float, latitude2: float, longitude2: float) -> float:
    """Distance in kilometres on a spherical Earth, never road distance/time."""
    for value, bound in ((latitude1, 90), (latitude2, 90), (longitude1, 180), (longitude2, 180)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > bound:
            raise ValueError("Coordinates must be finite, valid latitude/longitude values.")
    lat1, lon1, lat2, lon2 = map(math.radians, (latitude1, longitude1, latitude2, longitude2))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, a))))


def _number(value, name, *, integer=False):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                             or not math.isfinite(value) or value < 0 or (integer and type(value) is not int)):
        raise ValueError(f"{name} must be a finite non-negative {'integer' if integer else 'number'}.")


def _search(filename, city, radius_km, limit, project):
    """Apply a catalogue-specific projection, geographic filter, then a stable sort."""
    _number(radius_km, "radius_km")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer between 1 and 20.")
    if radius_km is not None and city is None:
        raise ValueError("A city is required with a radius.")
    cities = _cities()
    origin = None
    if city is not None:
        if not isinstance(city, str):
            raise ValueError("city must be a locality name or postal code string.")
        if _normalize(city) not in cities:
            return {"error": "City coordinates are unavailable in data/cities.json; provide a supported locality or postal code.",
                    "missing_coordinates": [city], "candidates": [], "total_matches": 0, "returned": 0,
                    "city": city, "radius_km": radius_km, "suggested_radius_km": None}
        origin = cities[_normalize(city)]
        radius_km = 30 if radius_km is None else radius_km
    candidates, missing = [], set()
    data = data_tools._load_dataset(filename)
    for record in data[data_tools._DATASETS[filename]]:
        candidate = project(record)
        if candidate is None:
            continue
        location = record["lieux"]
        names = [location["commune"]] if isinstance(location, dict) else [re.sub(r"\s*\(.*\)$", "", part).strip() for part in location.split("/")]
        points = []
        for name in names:
            point = cities.get(_normalize(name))
            if point:
                points.append(point)
            else:
                missing.add(name)
        distance = min((haversine(origin["latitude"], origin["longitude"], p["latitude"], p["longitude"])
                        for p in points), default=None) if origin else None
        if origin and (distance is None or distance > radius_km):
            continue
        candidate.update(id=record["id"], name=record["nom"], location=location,
                         distance_km=round(distance, 2) if distance is not None else None)
        candidates.append((distance or 0, candidate))
    candidates.sort(key=lambda item: (item[0], item[1]["price_eur"], item[1]["id"]))
    return {"candidates": [item[1] for item in candidates[:limit]], "total_matches": len(candidates),
            "returned": min(limit, len(candidates)), "city": city, "radius_km": radius_km,
            "suggested_radius_km": min(radius_km * 2 or 10, 150) if origin and not candidates and radius_km < 150 else None,
            "missing_coordinates": sorted(missing),
            "note": "Simulated catalogue prices; availability unknown. Distances are approximate locality-to-locality straight lines, not routes or travel fees. Radius was not expanded. Retrieve selected records for full constraints."}


def search_venues(city: str | None = None, radius_km: float | None = None,
                  min_capacity: int | None = None, max_price: float | None = None,
                  space: str = "indoor", configuration: str = "seated", rental: str = "day",
                  event_type: str | None = None, accessible: bool = False, limit: int = 15) -> dict:
    """Filter capacity in ONE configuration; price includes obligatory cleaning TTC."""
    _number(min_capacity, "min_capacity", integer=True)
    _number(max_price, "max_price")
    if space not in {"indoor", "outdoor"} or configuration not in {"seated", "standing"} or rental not in {"day", "evening"}:
        raise ValueError("Invalid space, configuration or rental period.")
    if type(accessible) is not bool:
        raise ValueError("accessible must be boolean.")

    def project(record):
        capacity = min(record["capacite"][{"indoor": "interieur", "outdoor": "exterieur"}[space]]
                       [{"seated": "personnes_assises", "standing": "personnes_debout"}[configuration]],
                       record["capacite"]["maximum_personnes_sur_site"])
        prices = record["prix"]
        rental_price = prices[{"day": "location_journee_ttc", "evening": "location_soiree_ttc"}[rental]]
        cleaning = prices["nettoyage_obligatoire_ttc"]
        if rental_price is None or cleaning is None:
            return None
        price = round(rental_price + cleaning, 2)
        if ((min_capacity is not None and capacity < min_capacity) or (max_price is not None and price > max_price)
                or (event_type is not None and event_type in record["types_evenements_interdits"])
                or (accessible and not record["equipements"]["acces_pmr"])):
            return None
        return {"capacity": capacity, "space": space, "configuration": configuration, "rental": rental,
                "price_eur": price, "tax_basis": "TTC", "type": record["type_lieu"],
                "ambiance": record["ambiance"],
                "equipment": [name for name, value in record["equipements"].items() if value is True],
                "constraints": record["contraintes"]}

    return _search("venues.json", city, radius_km, limit, project)


def search_caterers(city: str | None = None, radius_km: float | None = None,
                    attendance: int | None = None, max_price_per_person: float | None = None,
                    service_type: str | None = None, full_meal: bool = False,
                    dietary: list[str] | None = None, limit: int = 15) -> dict:
    """Filter formulas by attendance, service and TTC unit price; extras remain separate."""
    _number(attendance, "attendance", integer=True)
    _number(max_price_per_person, "max_price_per_person")
    if type(full_meal) is not bool or (dietary is not None and (not isinstance(dietary, list) or not all(isinstance(d, str) for d in dietary))):
        raise ValueError("full_meal must be boolean; dietary must be a list of option names.")

    def project(record):
        if any(record["options_alimentaires"].get(d) is not True for d in dietary or []):
            return None
        formulas = [{"id": f["id"], "service": f["type_service"], "full_meal": f["repas_complet"],
                     "price_per_person": f["prix_par_personne_ttc"],
                     "attendance": [f["minimum_personnes"], f["maximum_personnes"]]}
                    for f in record["formules"]
                    if (attendance is None or f["minimum_personnes"] <= attendance <= f["maximum_personnes"])
                    and (max_price_per_person is None or f["prix_par_personne_ttc"] <= max_price_per_person)
                    and (service_type is None or f["type_service"] == service_type) and (not full_meal or f["repas_complet"])]
        if not formulas:
            return None
        formulas.sort(key=lambda f: (f["price_per_person"], f["id"]))
        return {"price_eur": formulas[0]["price_per_person"], "price_unit": "per person, extras excluded",
                "tax_basis": "TTC", "formulas": formulas, "cuisines": record["styles_cuisine"],
                "dietary": [name for name, value in record["options_alimentaires"].items() if value is True],
                "intervention_zones": record["zones_intervention"]}

    return _search("caterers.json", city, radius_km, limit, project)


def search_providers(city: str | None = None, radius_km: float | None = None,
                     max_kit_price: float | None = None, services: list[str] | None = None,
                     limit: int = 15) -> dict:
    """Filter technical kits by HT price and actual son/light/video presence."""
    _number(max_kit_price, "max_kit_price")
    if services is not None and (not isinstance(services, list) or any(s not in ("son", "light", "video") for s in services)):
        raise ValueError("services must list son, light and/or video; other supplier categories are not in this catalogue.")

    def project(record):
        kits = [{k: kit[k] for k in ("id", "prix_estime_eur_ht", "son", "light", "video", "taille_montage")}
                for kit in record["kits"] if (max_kit_price is None or kit["prix_estime_eur_ht"] <= max_kit_price)
                and all(kit.get(s) not in (None, "null") for s in services or [])]
        if not kits:
            return None
        kits.sort(key=lambda k: (k["prix_estime_eur_ht"], k["id"]))
        return {"price_eur": kits[0]["prix_estime_eur_ht"], "price_unit": "kit, travel and tax excluded",
                "tax_basis": "HT", "kits": kits, "profile": record["profil_simulation"]}

    return _search("providers.json", city, radius_km, limit, project)
