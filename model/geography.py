"""Overpass et distance à vol d'oiseau, repris de WIP/fonction.py."""

import os
import json
import re
import unicodedata
import requests

from dotenv import load_dotenv
from functools import lru_cache
from math import radians, sin, cos, asin, sqrt, isfinite
from urllib.parse import urlsplit, urlunsplit

from data.cities.caterers import CITIES as CATERER_CITIES
from data.cities.providers import CITIES as PROVIDER_CITIES
from data.cities.venues import CITIES as VENUE_CITIES

load_dotenv()


CITIES = {
    "caterers": CATERER_CITIES,
    "providers": PROVIDER_CITIES,
    "venues": VENUE_CITIES
}


def _city_key(value):
    return " ".join("".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                            if not unicodedata.combining(char)).replace("-", " ").split())


def distance(lat1, lon1, lat2, lon2) -> float:
    """Calcule la distance à vol d'oiseau en kilomètres."""

    lat1, lon1, lat2, lon2 = map(
        radians, [lat1, lon1, lat2, lon2]
    )

    a = (
        sin((lat2 - lat1) / 2) ** 2
        + cos(lat1) * cos(lat2)
        * sin((lon2 - lon1) / 2) ** 2
    )

    return 6371 * 2 * asin(sqrt(min(1, a)))


def select_filtered(file_name: str, citie: str, x: int = 5, *, cities=None) -> list[str]:
    """Villes les plus proches ; adaptation de WIP/fonction.py aux candidats compatibles."""
    cities = CITIES[file_name] if cities is None else cities
    citie = citie.strip()
    if x <= 0 or not citie or not cities:
        return []
    citie = next((name for names in CITIES.values() for name in names
                  if _city_key(name) == _city_key(citie)), citie)
    endpoint = os.getenv("OVERPASS_URL", "").strip()
    if not endpoint:
        raise ValueError("OVERPASS_URL est absent du fichier .env.")
    return list(_rank_cities(citie, tuple(sorted(set(cities))), endpoint,
                            os.getenv("OVERPASS_KEY", "")))[:x]


@lru_cache(maxsize=128)
def _rank_cities(citie, cities, endpoint, api_key):
    # requests n'ajoute pas /interpreter à l'URL de base du serveur.
    url = urlsplit(endpoint)
    path = url.path.rstrip("/")
    if not path.endswith("/interpreter"):
        path += "/interpreter"
    endpoint = urlunsplit(url._replace(path=path))

    names = set((*cities, citie))

    pattern = "^(" + "|".join(map(re.escape, names)) + ")$"
    pattern = json.dumps(pattern, ensure_ascii=False)

    query = f"""
    [out:json][timeout:30];

    area["ISO3166-1"="BE"]["admin_level"="2"]->.belgium;

    (
        node["place"~"^(city|town|village|hamlet|suburb)$"]
            ["name"~{pattern},i](area.belgium);

        node["place"~"^(city|town|village|hamlet|suburb)$"]
            ["name:fr"~{pattern},i](area.belgium);

        node["place"~"^(city|town|village|hamlet|suburb)$"]
            ["name:nl"~{pattern},i](area.belgium);
    );

    out;
    """

    headers = {}

    if api_key:
        headers["X-API-Key"] = api_key

    try:
        response = requests.post(
            endpoint,
            headers=headers,
            data={"data": query},
            timeout=45
        )

        response.raise_for_status()
        data = response.json()

    except requests.HTTPError as error:
        raise RuntimeError(
            f"Overpass a renvoyé une erreur HTTP {error.response.status_code}."
        ) from None
    except requests.RequestException as error:
        # L'URL peut contenir une clé : ne pas l'inclure dans le message.
        raise RuntimeError(
            f"Impossible de joindre Overpass ({type(error).__name__})."
        ) from None
    except ValueError:
        raise RuntimeError("La réponse Overpass n'est pas un JSON valide.") from None

    if not isinstance(data, dict) or not isinstance(data.get("elements", []), list):
        raise RuntimeError("Réponse Overpass invalide.")
    if data.get("remark"):
        raise RuntimeError("Overpass signale une requête incomplète ou en erreur.")

    coordinates = {}

    for element in data.get("elements", []):
        if not isinstance(element, dict):
            raise RuntimeError("Élément Overpass invalide.")
        tags = element.get("tags", {})
        if not isinstance(tags, dict):
            raise RuntimeError("Noms de lieu Overpass invalides.")
        if not all(type(element.get(key)) in (int, float) and isfinite(element[key])
                   and abs(element[key]) <= maximum for key, maximum in (("lat", 90), ("lon", 180))):
            continue

        for key in ["name", "name:fr", "name:nl"]:

            name = tags.get(key)

            if isinstance(name, str) and name:
                coordinates.setdefault(
                    _city_key(name),
                    (element["lat"], element["lon"])
                )

    origin = coordinates.get(_city_key(citie))

    if origin is None:
        raise ValueError(f"Ville de départ introuvable dans Overpass : {citie!r}.")

    distances = {}

    for city in cities:

        position = coordinates.get(_city_key(city))

        if position is not None:
            distances[city] = distance(*origin, *position)

    if not distances:
        raise ValueError("Aucune ville candidate géolocalisée par Overpass.")
    return tuple(sorted(distances, key=distances.get))

