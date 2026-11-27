"""Catalogue searches and safe tools exposed to the event agents."""

import json
import logging
import math
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import requests

from .. import trace
from ..llm import tool_schema
from ..geography import select_filtered

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
LOGGER = logging.getLogger(__name__)


def _error(code, message):
    return {"error": message, "code": code}


def _normalize(value):
    value = unicodedata.normalize("NFKD", value.strip().casefold())
    return " ".join("".join(c for c in value if not unicodedata.combining(c))
                    .replace("_", " ").replace("-", " ").split())


def _contains(values, expected):
    return any(_normalize(value) == _normalize(expected) for value in values)


def _read_catalog(filename, key):
    try:
        with (DATA_DIR / filename).open(encoding="utf-8") as source:
            content = json.load(source)
        rows = content.get(key) if isinstance(content, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return _error("invalid_catalog", f"{filename} doit contenir une liste de dictionnaires '{key}'.")
        return rows
    except Exception as error:
        LOGGER.warning("Lecture de %s impossible : %s", filename, error)
        return _error("catalog_read_error", f"Impossible de lire {filename} : {error}")


def _validate_filters(filters, schema):
    """Validate the small subset of JSON Schema used by these search tools."""
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        return None, "Filtres attendus sous forme d'objet."
    properties = schema["function"]["parameters"]["properties"]
    cleaned = {}
    for name, value in filters.items():
        if name not in properties:
            return None, f"Filtre inconnu : {name}"
        if value is None:
            continue
        spec = properties[name]
        kind = spec["type"][0]
        valid = False
        if kind == "string":
            valid = isinstance(value, str)
            if valid and not value.strip():
                continue
        elif kind == "integer":
            valid = type(value) is int and value >= spec.get("minimum", 0)
        elif kind == "number":
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        elif kind == "boolean":
            valid = type(value) is bool
        elif kind == "array":
            valid = isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)
            if valid and not value:
                continue
        if valid and "enum" in spec:
            valid = _contains([x for x in spec["enum"] if x is not None], value)
        if not valid:
            return None, f"Paramètre invalide : {name}"
        cleaned[name] = value.strip() if isinstance(value, str) else value
    return cleaned, None


def _search(filename, key, filters, schema, matches, modifiers=()):
    rows = _read_catalog(filename, key)
    if isinstance(rows, dict):
        return rows
    try:
        filters, problem = _validate_filters(filters, schema)
        if problem:
            LOGGER.warning("%s : %s ; retour du catalogue complet", filename, problem)
            return rows
        if filename == "caterers.json" and "max_budget" in filters and "guests" not in filters:
            LOGGER.warning("Budget traiteur inexploitable sans guests ; retour du catalogue complet")
            return rows
        if not set(filters).difference({"limit", *modifiers}):
            return rows
        nonlocal_filters = {key: value for key, value in filters.items() if key != "city"}
        candidates = [row for row in rows if matches(row, nonlocal_filters)]
        try:
            candidates, _ = nearest_candidates(filename, candidates, filters.get("city"))
        except (ValueError, RuntimeError) as error:
            return _error("geography_unavailable", str(error))
        limit = filters.get("limit")
        return candidates[:limit] if limit is not None else candidates
    except Exception as error:
        # A readable catalogue remains usable if a nested field cannot be filtered.
        LOGGER.warning("Filtrage de %s impossible : %s ; retour du catalogue complet", filename, error)
        return rows


def row_cities(row):
    location = row["lieux"]
    if isinstance(location, dict):
        return [location["commune"]]
    return [city.strip() for city in re.split(r"[/()]", location) if city.strip()]


def nearest_candidates(filename, candidates, city, *, strict=False):
    """Retenir la commune compatible la plus proche, sans relâcher les autres critères."""
    if not city or not candidates:
        return candidates, None
    local = [row for row in candidates if _contains(row_cities(row), city)]
    selected_city = city
    if not local and not strict:
        cities = sorted({name for row in candidates for name in row_cities(row)})
        closest = select_filtered(Path(filename).stem, city, x=len(cities), cities=cities)
        if not closest:
            raise ValueError("Aucune ville candidate géolocalisée.")
        selected_city = closest[0]
        local = [row for row in candidates if _contains(row_cities(row), selected_city)]
    return local, {"requested_city": city, "selected_city": selected_city,
                   "expanded": _normalize(city) != _normalize(selected_city),
                   "basis": "Distance à vol d'oiseau entre communes géolocalisées (Overpass), pas un trajet routier."}


def _common_match(row, filters):
    if "ids" in filters and row["id"] not in filters["ids"]:
        return False
    if "event_type" in filters:
        if not _contains(row["types_evenements_adaptes"], filters["event_type"]):
            return False
        if _contains(row.get("types_evenements_interdits", []), filters["event_type"]):
            return False
    return True


def _caterer_match(row, filters):
    if not _common_match(row, filters):
        return False
    if "city" in filters and not _contains([row["lieux"]["commune"]], filters["city"]):
        return False
    if "province" in filters and not _contains(row["zones_intervention"], filters["province"]):
        return False
    if "cuisine" in filters and not _contains(row["styles_cuisine"], filters["cuisine"]):
        return False
    for option in filters.get("dietary_options", []):
        if not any(_normalize(key) == _normalize(option) and value is True
                   for key, value in row["options_alimentaires"].items()):
            return False

    def formula_matches(formula):
        if "service_type" in filters and not _contains([formula["type_service"]], filters["service_type"]):
            return False
        if "full_meal" in filters and formula["repas_complet"] != filters["full_meal"]:
            return False
        if "guests" in filters and not formula["minimum_personnes"] <= filters["guests"] <= formula["maximum_personnes"]:
            return False
        price = formula["prix_par_personne_ttc"]
        if "max_price_per_person" in filters and price > filters["max_price_per_person"]:
            return False
        if "max_budget" in filters:
            total = price * filters["guests"] + row["frais_supplementaires"]["livraison_forfait_ttc"]
            if total > filters["max_budget"]:
                return False
        return True

    return any(formula_matches(formula) for formula in row["formules"])


def get_caterers(city: str | None = None, province: str | None = None,
                 event_type: str | None = None, guests: int | None = None,
                 max_price_per_person: float | None = None, max_budget: float | None = None,
                 service_type: str | None = None, full_meal: bool | None = None,
                 cuisine: str | None = None, dietary_options: list[str] | None = None,
                 ids: list[str] | None = None, limit: int | None = 5) -> list[dict] | dict:
    """Match a single formula; budget is TTC food + delivery, before variable extras."""
    return _search("caterers.json", "traiteurs", locals(), SCHEMA_CATERERS, _caterer_match)


# The catalogue's spelling is intentional. 'null' means absent, not unknown.
LEVELS = ["trés nul", "simple", "normal", "bien", "trés bien", "exelent", "parfait"]
SETUP_SIZES = ["trés petit", "petit", "normal", "grand", "trés grand", "geant"]


def _level_matches(actual, requested):
    if actual is None:
        return False
    if _normalize(requested) == "null":
        return actual == "null"
    levels = [_normalize(level) for level in LEVELS]
    return _normalize(actual) in levels and levels.index(_normalize(actual)) >= levels.index(_normalize(requested))


def _provider_match(row, filters):
    if not _common_match(row, filters):
        return False
    for parameter, field in (("city", "lieux"), ("province", "province")):
        if parameter in filters:
            value = row.get(field)
            pattern = r"(?<!\w)" + re.escape(_normalize(filters[parameter])) + r"(?!\w)"
            if not isinstance(value, str) or not re.search(pattern, _normalize(value)):
                return False
    if "category" in filters and not _contains([row.get("categorie_taille", "")], filters["category"]):
        return False

    def kit_matches(kit):
        if "max_budget" in filters and kit["prix_estime_eur_ht"] > filters["max_budget"]:
            return False
        if "setup_size" in filters and not _contains([kit["taille_montage"]], filters["setup_size"]):
            return False
        for parameter, field in (("sound_level", "son"), ("light_level", "light"), ("video_level", "video")):
            if parameter in filters and not _level_matches(kit[field], filters[parameter]):
                return False
        return True

    return any(kit_matches(kit) for kit in row["kits"])


def get_providers(city: str | None = None, province: str | None = None,
                  category: str | None = None, max_budget: float | None = None,
                  sound_level: str | None = None, light_level: str | None = None,
                  video_level: str | None = None, setup_size: str | None = None,
                  ids: list[str] | None = None, limit: int | None = 5) -> list[dict] | dict:
    """Match all technical requirements on one kit, with its estimated HT price."""
    return _search("providers.json", "prestataires", locals(), SCHEMA_PROVIDERS, _provider_match)


def _venue_match(row, filters):
    if not _common_match(row, filters):
        return False
    for parameter, field in (("city", "commune"), ("province", "province")):
        if parameter in filters and not _contains([row["lieux"][field]], filters[parameter]):
            return False
    if "venue_type" in filters and not _contains([row["type_lieu"]], filters["venue_type"]):
        return False
    space = _normalize(filters.get("space_type", "les deux"))
    if "space_type" in filters and _normalize(row["type_espace"]) not in (space, "les deux"):
        return False
    if "guests" in filters:
        capacity = row["capacite"]["maximum_personnes_sur_site"]
        if "seated" in filters or "space_type" in filters:
            key = "personnes_assises" if filters.get("seated") else "personnes_debout"
            spaces = ("interieur", "exterieur") if space == "les deux" else (space,)
            capacity = min(capacity, max(row["capacite"][s][key] for s in spaces))
        if capacity < filters["guests"]:
            return False
    if "max_budget" in filters:
        period = _normalize(filters.get("rental_period", "journee"))
        price = row["prix"][f"location_{period}_ttc"] + row["prix"]["nettoyage_obligatoire_ttc"]
        if price > filters["max_budget"]:
            return False
    if "accessible" in filters and row["equipements"]["acces_pmr"] != filters["accessible"]:
        return False
    for parameter, imposed, allowed in (
        ("caterer_id", "traiteur_impose", "traiteurs_autorises_ids"),
        ("provider_id", "prestataire_technique_impose", "prestataires_techniques_autorises_ids"),
    ):
        if parameter in filters and row["contraintes"][imposed] and filters[parameter] not in row["contraintes"][allowed]:
            return False
    return True


def get_venues(city: str | None = None, province: str | None = None,
               event_type: str | None = None, guests: int | None = None,
               max_budget: float | None = None, rental_period: str | None = None,
               venue_type: str | None = None, space_type: str | None = None,
               seated: bool | None = None, accessible: bool | None = None,
               caterer_id: str | None = None, provider_id: str | None = None,
               ids: list[str] | None = None, limit: int | None = 5) -> list[dict] | dict:
    """Budget includes rental + mandatory cleaning, excluding refundable deposit."""
    return _search("venues.json", "salles", locals(), SCHEMA_VENUES, _venue_match,
                   modifiers=("rental_period", "seated"))


def ask_user(message: str) -> dict:
    """Display the complete draft and request approval; never send anything."""
    try:
        if not isinstance(message, str) or not message.strip():
            LOGGER.warning("Validation refusée : message vide ou invalide")
            return {"approved": False}
        trace.console.print(message, markup=False, highlight=False)
        while True:
            answer = trace.console.input("Validez-vous ce contenu ? (oui/non) : ").strip().casefold()
            if answer in ("oui", "o", "yes", "y"):
                return {"approved": True}
            if answer in ("non", "n", "no"):
                return {"approved": False}
            trace.console.print("Réponse invalide. Veuillez saisir oui ou non.")
    except (Exception, KeyboardInterrupt) as error:
        LOGGER.warning("Validation utilisateur interrompue : %s", error)
        return {"approved": False}


DAYS = {day: index for index, day in enumerate(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}


def _parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def get_next_day(day: str, from_date: str | None = None) -> str | dict:
    """Return the requested weekday strictly after the reference date."""
    try:
        if not isinstance(day, str) or day.strip().casefold() not in DAYS:
            return _error("invalid_day", "Jour invalide : utilisez monday à sunday.")
        start = date.today() if from_date is None else _parse_date(from_date)
        if start is None:
            return _error("invalid_date", "Date de référence invalide : format YYYY-MM-DD attendu.")
        delta = (DAYS[day.strip().casefold()] - start.weekday()) % 7 or 7
        return (start + timedelta(days=delta)).isoformat()
    except Exception as error:
        return _error("date_error", f"Calcul de date impossible : {error}")


WEATHER_FIELDS = {
    "temperature_min": "temperature_2m_min",
    "temperature_max": "temperature_2m_max",
    "precipitation_mm": "precipitation_sum",
    "rain_probability": "precipitation_probability_max",
    "wind_max_kmh": "wind_speed_10m_max",
    "weather_code": "weather_code",
}


def get_meteo(day: str, city: str) -> dict:
    """Fetch and verify all daily values for the requested date (up to 16 days)."""
    try:
        event_date = _parse_date(day)
        if event_date is None:
            return _error("invalid_date", "Date invalide : format YYYY-MM-DD attendu.")
        if not isinstance(city, str) or not city.strip():
            return _error("invalid_city", "La ville doit être une chaîne non vide.")
        days_ahead = (event_date - date.today()).days
        if not 0 <= days_ahead < 16:
            return _error("forecast_unavailable", "Prévisions indisponibles : date passée ou au-delà des 16 jours (aujourd'hui inclus).")
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city.strip(), "count": 1, "language": "fr"}, timeout=10,
        )
        geo.raise_for_status()
        geodata = geo.json()
        if not isinstance(geodata, dict):
            return _error("invalid_api_response", "Réponse de géocodage invalide.")
        if geodata.get("error"):
            return _error("api_error", f"Erreur Open-Meteo : {geodata.get('reason', 'géocodage impossible')}")
        locations = geodata.get("results", [])
        if not isinstance(locations, list):
            return _error("invalid_api_response", "Liste de villes invalide.")
        if not locations:
            return _error("unknown_city", f"Ville introuvable : {city}")
        location = locations[0]
        if not isinstance(location, dict) or not isinstance(location.get("name"), str):
            return _error("invalid_api_response", "Ville retournée invalide.")
        for coordinate, maximum in (("latitude", 90), ("longitude", 180)):
            value = location.get(coordinate)
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > maximum:
                return _error("invalid_api_response", "Coordonnées retournées invalides.")
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": location["latitude"], "longitude": location["longitude"],
                "daily": ",".join(WEATHER_FIELDS.values()), "timezone": "auto",
                "start_date": day, "end_date": day,
                "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm",
            }, timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return _error("invalid_api_response", "Réponse météo invalide.")
        if data.get("error"):
            return _error("api_error", f"Erreur Open-Meteo : {data.get('reason', 'prévisions indisponibles')}")
        daily = data.get("daily")
        if not isinstance(daily, dict) or not isinstance(daily.get("time"), list) or day not in daily["time"]:
            return _error("forecast_unavailable", "Prévisions indisponibles pour cette date.")
        index = daily["time"].index(day)
        result = {"city": location["name"], "country": location.get("country"), "date": day}
        for output, field in WEATHER_FIELDS.items():
            values = daily.get(field)
            if not isinstance(values, list) or len(values) <= index:
                return _error("forecast_unavailable", f"Prévision manquante : {field}.")
            value = values[index]
            if type(value) not in (int, float) or not math.isfinite(value):
                return _error("forecast_unavailable", f"Prévision indisponible : {field}.")
            result[output] = value
        result["source"] = "Open-Meteo"
        return result
    except requests.RequestException as error:
        return _error("api_error", f"Erreur API Open-Meteo : {error}")
    except Exception as error:
        return _error("invalid_api_response", f"Impossible de traiter la réponse Open-Meteo : {error}")


def _optional(kind, description, **constraints):
    if "enum" in constraints:
        constraints["enum"] = [*constraints["enum"], None]
    return {"type": [kind, "null"], "description": description, **constraints}


_CITY = _optional("string", "Commune de référence : sur place, puis commune compatible la plus proche à vol d'oiseau. Pour les services, utiliser la commune réelle de la salle retenue.")
_EVENT = _optional("string", "Type d'événement adapté, par exemple mariage, concert ou reception_entreprise.")
_GUESTS = _optional("integer", "Nombre de participants.", minimum=1)
_IDS = _optional("array", "Identifiants exacts autorisés ; conserver les zéros initiaux.", items={"type": "string"})
_LIMIT = _optional("integer", "Maximum de candidats filtrés (5 par défaut, null = tous). Jamais appliqué au repli.", minimum=1)
_FALLBACK = " Fiches originales complètes. Sans filtre exploitable ou filtre invalide : catalogue intégral. Aucun résultat compatible : liste vide ; géolocalisation indisponible : erreur explicite."

SCHEMA_CATERERS = tool_schema("get_caterers", "Recherche les traiteurs ; tous les critères de formule doivent correspondre à une même formule." + _FALLBACK, {
    "city": _CITY,
    "province": _optional("string", "Province de l'événement, à rechercher dans zones_intervention."),
    "event_type": _EVENT, "guests": _GUESTS,
    "max_price_per_person": _optional("number", "Plafond TTC par personne pour la formule seule.", minimum=0),
    "max_budget": _optional("number", "Plafond TTC formule × guests + livraison. Nécessite guests. Hors boissons, personnel supplémentaire et kilomètres, à vérifier séparément.", minimum=0),
    "service_type": _optional("string", "Type de service.", enum=["cocktail", "buffet", "service_a_table", "food_truck", "barbecue"]),
    "full_meal": _optional("boolean", "Exiger (true) ou exclure (false) une formule repas complet."),
    "cuisine": _optional("string", "Style de cuisine, par exemple belge ou vegetale."),
    "dietary_options": _optional("array", "Toutes les options requises : vegan, vegetarien, sans_gluten, sans_lactose, halal, casher, etc.", items={"type": "string"}),
    "ids": _IDS, "limit": _LIMIT,
}, requis=[])

SCHEMA_PROVIDERS = tool_schema("get_providers", "Recherche des prestataires techniques ; tous les critères doivent correspondre à un même kit." + _FALLBACK, {
    "city": _CITY,
    "province": _optional("string", "Province d'implantation ; les fiches sans province ne correspondent pas."),
    "category": _optional("string", "Catégorie de taille renseignée.", enum=["micro", "petite", "moyenne", "grande"]),
    "max_budget": _optional("number", "Plafond du prix estimé du kit en EUR HT, hors déplacement et TVA.", minimum=0),
    **{name: _optional("string", "Niveau minimum du kit selon le référentiel. Chaîne 'null' = prestation absente ; null JSON = pas de filtre.", enum=["null", *LEVELS])
       for name in ("sound_level", "light_level", "video_level")},
    "setup_size": _optional("string", "Taille exacte de montage.", enum=SETUP_SIZES),
    "ids": _IDS, "limit": _LIMIT,
}, requis=[])

SCHEMA_VENUES = tool_schema("get_venues", "Recherche les salles adaptées avec capacités et exclusivités commerciales." + _FALLBACK, {
    "city": _CITY, "province": _optional("string", "Province de la salle."),
    "event_type": _EVENT, "guests": _GUESTS,
    "max_budget": _optional("number", "Plafond EUR TTC location + nettoyage obligatoire, hors caution remboursable.", minimum=0),
    "rental_period": _optional("string", "Tarif à utiliser ; journee par défaut.", enum=["journee", "soiree"]),
    "venue_type": _optional("string", "Type de lieu, par exemple domaine, chateau, loft ou salle_polyvalente."),
    "space_type": _optional("string", "Espace requis. Les lieux mixtes acceptent intérieur ou extérieur.", enum=["interieur", "exterieur", "les deux"]),
    "seated": _optional("boolean", "Capacité assise si true, debout si false. Sans précision : maximum sur site. Jamais de cumul intérieur/extérieur."),
    "accessible": _optional("boolean", "Valeur requise pour acces_pmr."),
    "caterer_id": _optional("string", "Traiteur choisi : vérifier les exclusivités de la salle."),
    "provider_id": _optional("string", "Prestataire choisi : vérifier les exclusivités (identifiant exact, ex. 001)."),
    "ids": _IDS, "limit": _LIMIT,
}, requis=[])

SCHEMA_ASK_USER = tool_schema("ask_user", "Affiche le contenu complet à valider et demande oui/non. Ne réalise aucune action externe.", {
    "message": {"type": "string", "description": "Contenu complet à présenter à l'utilisateur pour validation."},
})

SCHEMA_NEXT_DAY = tool_schema("get_next_day", "Prochaine occurrence du jour strictement après la date de référence, calculée localement.", {
    "day": {"type": "string", "enum": list(DAYS), "description": "Jour de la semaine en anglais."},
    "from_date": _optional("string", "Date de référence YYYY-MM-DD ; aujourd'hui si omise ou null."),
}, requis=["day"])

SCHEMA_METEO = tool_schema("get_meteo", "Prévisions Open-Meteo pour aujourd'hui ou une date future dans les 16 jours, sous réserve de disponibilité réelle.", {
    "day": {"type": "string", "description": "Date au format YYYY-MM-DD."},
    "city": {"type": "string", "description": "Ville de l'événement."},
})
