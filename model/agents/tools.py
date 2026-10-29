from ..llm import tool_schema

import requests
from datetime import date

def get_meteo(day : str, city : str) -> dict :

    try :
        date_event = date.fromisoformat(day)
    except ValueError :
        return {"error" : "date invalide. supported format : YYYY-MM-DD"}

    if date_event > date.today() :
        return {"error" : "date is passed"}

    try :
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": city,
                "count": 1,
                "language": "fr"
            },
            timeout=10
        )
        geo.raise_for_status()

        locations = geo.json().get("results", [])

        if not locations:
            return {"error": f"Ville introuvable : {city}"}

        location = locations[0]

        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "daily": ",".join([
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_sum",
                    "precipitation_probability_max",
                    "wind_speed_10m_max",
                    "weather_code"
                ]),
                "timezone": "auto",
                "start_date": day,
                "end_date": day
            },
            timeout=10
        )
        response.raise_for_status()

        data = response.json()
        daily = data.get("daily", {})

        if not daily.get("time"):
            return {
                "error": "Prévisions indisponibles pour cette date. "
                         "La limite habituelle est de 16 jours."
            }

        return {
            "city": location["name"],
            "country": location.get("country"),
            "date": day,
            "temperature_min": daily["temperature_2m_min"][0],
            "temperature_max": daily["temperature_2m_max"][0],
            "precipitation_mm": daily["precipitation_sum"][0],
            "rain_probability": daily["precipitation_probability_max"][0],
            "wind_max_kmh": daily["wind_speed_10m_max"][0],
            "weather_code": daily["weather_code"][0],
            "source": "Open-Meteo"
        }
    
    except requests.RequestException as error :
        return {"error" : f"Erreur API Open-Meteo : {error}"}

def get_caterers() -> dict :
    ...

def get_providers() -> dict :
    ...

def get_venues() -> dict :
    ...

# ---------------------------------------------------------------

SCHEMA_METEO = tool_schema(
    "get_meteo",
    "Obtient les prévisions météorologiques pour une date.",
    {
        "day": {
            "type": "string",
            "description": "Date au format YYYY-MM-DD"
        },
        "city" : {
            "type" : "string",
            "description" : "location of the event"
        }
    }
)

SCHEMA_CATERERS = tool_schema(
    "get_caterers",
    "Récupère les traiteurs disponibles.",
    {}
)

SCHEMA_PROVIDERS = tool_schema(
    "get_providers",
    "Récupère les prestataires disponibles.",
    {}
)

SCHEMA_VENUES = tool_schema(
    "get_venues",
    "Récupère les salles disponibles.",
    {}
)