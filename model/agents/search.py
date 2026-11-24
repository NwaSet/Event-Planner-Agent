"""Vues destinées aux agents ; les recherches publiques gardent leur contrat brut."""

from copy import deepcopy
from datetime import datetime, timedelta

from . import tools


CATALOGS = {
    "venue": ("venues.json", "salles", tools.SCHEMA_VENUES, tools._venue_match),
    "catering": ("caterers.json", "traiteurs", tools.SCHEMA_CATERERS, tools._caterer_match),
    "provider": ("providers.json", "prestataires", tools.SCHEMA_PROVIDERS, tools._provider_match),
}


def load_catalogs():
    return {category: tools._read_catalog(filename, key)
            for category, (filename, key, _, _) in CATALOGS.items()}


def check_schedule(planning):
    if not isinstance(planning, dict) or set(planning) != {"start", "end"}:
        raise ValueError("Planning attendu : start et end uniquement.")
    # Horaires locaux explicites : ne pas mélanger dates seules et fuseaux implicites.
    for value in planning.values():
        if not isinstance(value, str) or len(value) != 16 or value[10] != "T":
            raise ValueError("Utilisez YYYY-MM-DDTHH:MM, heure locale de l'événement.")
    start, end = (datetime.fromisoformat(planning[key]) for key in ("start", "end"))
    if start >= end:
        raise ValueError("La fin doit être strictement postérieure au début.")
    return start, end


def compatible(category, row, context):
    """Contraintes objectives qui ne figurent pas dans les filtres publics."""
    planning = context.get("planning")
    venue = context.get("venue")
    if category == "venue" and planning:
        start, end = check_schedule(planning)
        # En l'absence de fin de musique distincte, la soirée musicale finit avec l'événement.
        music_end = context.get("music_end")
        if music_end:
            end = datetime.fromisoformat(music_end)
            if not start <= end <= datetime.fromisoformat(planning["end"]):
                return False
        if context.get("music_required", True):
            deadline = datetime.combine(start.date(), datetime.strptime(
                row["contraintes"]["heure_fin_musique"], "%H:%M").time())
            if deadline <= start:
                deadline += timedelta(days=1)
            if end > deadline:
                return False
    if category in ("catering", "provider") and venue:
        imposed, ids = (("traiteur_impose", "traiteurs_autorises_ids") if category == "catering"
                        else ("prestataire_technique_impose", "prestataires_techniques_autorises_ids"))
        if venue["contraintes"][imposed] and row["id"] not in venue["contraintes"][ids]:
            return False
    if category == "catering":
        if (planning or context.get("date")) and context.get("today"):
            start = check_schedule(planning)[0] if planning else datetime.fromisoformat(context["date"])
            today = datetime.fromisoformat(context["today"]).date()
            if (start.date() - today).days < row["reservation"]["delai_minimum_jours"]:
                return False
        if venue:
            if not tools._contains(row["zones_intervention"], venue["lieux"]["province"]):
                return False
            for required, equipment in (("cuisine_sur_place_necessaire", "cuisine_traiteur"),
                                        ("eau_potable_necessaire", "eau_potable"),
                                        ("electricite_necessaire", "electricite")):
                if row["logistique"][required] and not venue["equipements"].get(equipment):
                    return False
            if context.get("space_type") == "exterieur":
                if not row["logistique"]["installation_exterieure_possible"]:
                    return False
                if (row["logistique"]["espace_couvert_obligatoire_en_exterieur"]
                        and not venue["equipements"].get("espace_traiteur_couvert")):
                    return False
    return True


def project(category, row):
    """Garder prix, offres, critères qualitatifs et contraintes ; supprimer le superflu."""
    fields = {
        "venue": ("id", "nom", "lieux", "type_lieu", "type_espace", "capacite", "prix",
                  "types_evenements_adaptes", "types_evenements_interdits", "ambiance",
                  "meteo", "equipements", "contraintes"),
        "catering": ("id", "nom", "lieux", "zones_intervention", "styles_cuisine", "gamme",
                     "types_evenements_adaptes", "formules", "options_alimentaires", "boissons",
                     "frais_supplementaires", "logistique", "reservation"),
        "provider": ("id", "nom", "lieux", "province", "categorie_taille", "profil_simulation",
                     "kits", "deplacement"),
    }
    result = {key: deepcopy(row[key]) for key in fields[category] if key in row}
    # Description d'offre et gamme conservées pour permettre un véritable choix qualitatif.
    if category == "catering":
        for formula in result["formules"]:
            formula.pop("nom", None)  # La description plus précise reste présente.
    return result


def search_candidates(category, filters=None, *, catalogs=None, context=None, accept=None):
    """Aucun repli complet : diagnostics explicites et offres compatibles uniquement."""
    filename, key, schema, matches = CATALOGS[category]
    rows = catalogs[category] if catalogs is not None else tools._read_catalog(filename, key)
    if isinstance(rows, dict):
        return {"status": "error", **rows, "candidates": []}
    try:
        cleaned, problem = tools._validate_filters(filters, schema)
        if problem:
            return {"status": "invalid_filters", "error": problem, "candidates": []}
        if category == "catering" and "max_budget" in cleaned and "guests" not in cleaned:
            return {"status": "invalid_filters", "error": "Budget traiteur : effectif nécessaire.", "candidates": []}
        # Une liste d'identifiants imposée vide signifie un conflit, pas l'absence de filtre.
        if filters is not None and filters.get("ids") == []:
            return {"status": "no_match", "error": "Aucun identifiant autorisé.", "candidates": []}
        candidates = []
        for row in rows:
            if not matches(row, cleaned) or not compatible(category, row, context or {}):
                continue
            if accept is not None and not accept(row):
                continue
            candidate = row
            offers_key = {"catering": "formules", "provider": "kits"}.get(category)
            if offers_key:
                candidate = dict(row)
                candidate[offers_key] = [offer for offer in row[offers_key]
                                        if matches({**row, offers_key: [offer]}, cleaned)]
            candidates.append(project(category, candidate))
        # Sous-ensemble explicite : le modèle peut préciser ses critères pour explorer davantage.
        limit = min(cleaned.get("limit") or 5, 10)
        return {"status": "matched" if candidates else "no_match",
                "total": len(candidates), "shown": min(limit, len(candidates)),
                "candidates": candidates[:limit], "availability": "non vérifiée",
                **({"error": "Aucune offre ne respecte toutes les contraintes."} if not candidates else {})}
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return {"status": "error", "error": f"Recherche impossible : {error}", "candidates": []}


def compact_tool(category):
    """Outil autonome des spécialistes ; l'orchestrateur ajoute ses contraintes verrouillées."""
    def search(**filters):
        return search_candidates(category, filters)

    schema = deepcopy(CATALOGS[category][2])
    schema["function"]["description"] = (
        "Recherche compacte avec statut matched/no_match/invalid_filters/error. "
        "Aucun repli intégral. Offres compatibles seulement ; disponibilité non vérifiée.")
    schema["function"]["parameters"]["properties"]["limit"]["description"] = "Candidats affichés, entre 1 et 10."
    return search, schema
