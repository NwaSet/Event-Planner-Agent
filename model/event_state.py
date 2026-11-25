"""État métier d'un événement et contrôles d'un tour ; persistance assurée par le DAO."""

import json
import math
import re
from copy import deepcopy
from datetime import date, datetime

from .agents import tools
from .finance import FINANCIAL_FIELDS, amount, money, venue_cost, catering_cost, provider_cost, financial_lines
from .agents.search import (CATALOGS, check_schedule, compact_tool, load_catalogs, search_candidates,
                            compatible, normalize_arguments, event_type_error)


TASKS = ("planning", "budget", "venue", "catering", "provider", "documents")
REQUIREMENT_PROPERTIES = {
    "city": {"type": "string"}, "province": {"type": "string"},
    "location_scope": {"type": "string", "enum": ["city", "province_fallback"],
                       "description": "city par défaut/ville imposée ; province_fallback seulement pour une localisation explicitement souple, ville d'abord puis même province faute de données de voisinage."},
    "event_type": {"type": "string", "description": "Catégorie précise du catalogue seulement."},
    "guests": {"type": "integer", "minimum": 1},
    "budget_total": {"type": ["number", "null"], "minimum": 0},
    "date": {"type": "string", "description": "Date imposée YYYY-MM-DD."},
    "weekday": {"type": "string", "enum": list(tools.DAYS), "description": "Jour demandé, sans inventer une date."},
    "rental_period": {"type": "string", "enum": ["journee", "soiree"]},
    "planning": {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
                 "required": ["start", "end"], "additionalProperties": False},
    "music_required": {"type": "boolean", "description": "Musique jusqu'à la fin, true par défaut."},
    "music_end": {"type": "string", "description": "Fin de musique imposée YYYY-MM-DDTHH:MM si distincte."},
    "preferences": {"type": "string"}, "assumptions": {"type": "string"},
    "document_kind": {"type": "string", "enum": ["email", "synthesis"]},
    "catering_distance_km": {"type": "number"},
    "provider_distance_km": {"type": "number"},
    "provider_vehicles": {"type": "integer"},
    "provider_vat_rate": {"type": "number"},
    **{category + "_filters": schema["function"]["parameters"]
       for category, (_, _, schema, _) in CATALOGS.items()},
}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def describe_filters(filters):
    labels = {"city": "ville", "province": "province", "event_type": "type d'événement",
              "guests": "effectif", "max_budget": "plafond en EUR", "rental_period": "location",
              "ids": "identifiants autorisés", "accessible": "accès PMR", "seated": "places assises",
              "space_type": "espace", "caterer_id": "traiteur imposé", "provider_id": "prestataire imposé"}

    def display(value):
        if isinstance(value, list):
            return ", ".join(display(item) for item in value) or "aucun"
        if isinstance(value, bool):
            return "oui" if value else "non"
        return {"soiree_etudiante": "soirée étudiante", "soiree": "soirée", "journee": "journée"}.get(
            str(value), str(value).replace("_", " "))

    return "; ".join(f"{labels.get(key, key.replace('_', ' '))} : {display(value)}"
                     for key, value in filters.items() if key != "limit") or "critères demandés"


def normalize_preferences(value):
    """Un seul format transmis aux agents : texte, sans perdre les clés d'un objet."""
    return encode(value) if isinstance(value, dict) else value


class TaskImpossible(Exception):
    """Un spécialiste signale un conflit explicite : aucune réparation JSON à tenter."""


class ConstraintRevisionRequired(ValueError):
    """Une délégation ne peut modifier implicitement une exigence persistante."""


def parse_object(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Un objet JSON est attendu.")
    if isinstance(value.get("error"), str) and value["error"].strip():
        raise TaskImpossible(value["error"])
    return value


class EventRun:
    def __init__(self, request, max_llm_calls=24, max_delegations=8, *, event_data=None, interactive=False):
        self.interactive = interactive
        self.turn_request = request
        self.turn_actions = False
        self.direct_reply = None
        self.answered_question = None
        self.resume_writer = None
        self.search_filters = {}  # Essais de recherche du tour, jamais des exigences persistantes.
        self.event_data = {"request": request, "requirements": {}, "planning": None,
                           "budget": None, "venue": None, "catering": None,
                           "providers": [], "documents": [], "tasks": {}, "plan_approval": None, "weather": [],
                           "location_search": None, "pending_input": None, "revision": 0,
                           "document_archive": [], "decision_archive": [], "approval_archive": [],
                           "selection_filters": {}, "rejected_venues": [], "venue_replacement_pending": False}
        if event_data:
            self.event_data.update(deepcopy(event_data))
        self.catalogs = load_catalogs()
        # Garde-fou ciblé : conserver une catégorie explicitement nommée dans la demande.
        # Les autres interprétations et les choix qualitatifs restent confiés aux agents.
        wording = tools._normalize(request) if not event_data else ""
        student_event = r"\b(?:soirees? etudiantes?|evenements? etudiants?)\b"
        if re.search(student_event, wording) and not re.search(
                r"\b(?:pas|sans|hors|non) (?:une? |de |des? )?" + student_event, wording):
            self.event_data["requirements"]["event_type"] = "soiree_etudiante"
        # Filet ciblé pour les faits littéraux omis par le Supervisor. Pas de choix
        # qualitatif ni d'interprétation des formulations ambiguës ici.
        req = self.event_data["requirements"]
        cities = {row["lieux"]["commune"] for row in self.catalogs["venue"]} if isinstance(self.catalogs["venue"], list) else set()
        mentioned = [city for city in cities
                     if re.search(r"\ba\s+" + re.escape(tools._normalize(city)) + r"\b", wording)
                     and not re.search(r"\b(?:pas|sauf)\s+a\s+" + re.escape(tools._normalize(city)) + r"\b", wording)]
        if len(mentioned) == 1:
            req["city"] = mentioned[0]
        locations = []
        for city in cities:
            name = re.escape(tools._normalize(city)) + r"\b"
            strict = re.search(r"\b(?:uniquement a |obligatoirement (?:dans la commune de |a )|pas en dehors de )" + name, wording)
            soft = re.search(r"\b(?:dans le coin de |autour de |pres de |dans les environs de )" + name, wording)
            negated = re.search(r"\b(?:pas |sans )(?:dans le coin de |autour de |pres de |dans les environs de )" + name, wording)
            if strict or (soft and not negated):
                locations.append((city, "city" if strict else "province_fallback"))
        if len(locations) == 1 and ("city" not in req or req["city"] == locations[0][0]):
            req["city"], req["location_scope"] = locations[0]
        headcounts = re.findall(r"\b(?:pour|de)\s+(\d+)\s+(?:personnes|invites|participants)\b", wording)
        if len(set(headcounts)) == 1 and int(headcounts[0]) > 0:
            req["guests"] = int(headcounts[0])
        if re.search(r"\bpour (?:un |une? )?anniversaire\b", wording):
            req.setdefault("event_type", "anniversaire")
        # Une rédaction explicitement demandée ne peut disparaître de la liste des tâches.
        email_requested = re.search(r"\b(?:redige|rediger|ecris|ecrire|prepare|preparer)\s+(?:un |une? |l')?(?:e-?mail|courriel)\b", wording)
        if email_requested and not re.search(r"\b(?:ne|pas|sans)\s+(?:\w+\s+){0,2}(?:redige|rediger|ecris|ecrire|prepare|preparer)\b", wording):
            req["document_kind"] = "email"
            self.event_data["tasks"]["documents"] = {"status": "pending"}
            if re.search(r"\b(?:une|la) salle\b", wording):
                self.event_data["tasks"]["venue"] = {"status": "pending"}
        # Un compteur et le dernier contexte utile par tâche, sans cache de résultats.
        self.attempts = {}
        self.scope_declared = False
        self.today = date.today().isoformat()
        self.counters = {"llm_calls": 0, "delegations": 0,
                         "max_llm_calls": max_llm_calls, "max_delegations": max_delegations}

    def remember(self, requirements=None, tasks=None):
        """Mémoriser les seuls éléments fournis avec une délégation, sans étape initiale."""
        if requirements is None and tasks is None:
            return
        if requirements is None:
            requirements = {}
        if isinstance(requirements, dict) and "tasks" in requirements:
            nested = requirements["tasks"]
            if not isinstance(nested, list) or not nested or any(task not in TASKS for task in nested):
                raise ValueError("requirements.tasks : liste non vide de tâches connues attendue.")
            if tasks is not None and (not isinstance(tasks, list) or any(task not in TASKS for task in tasks)
                                      or set(tasks) != set(nested)):
                raise ValueError("Listes tasks contradictoires entre la délégation et requirements.")
            tasks = nested if tasks is None else tasks
            requirements = {key: value for key, value in requirements.items() if key != "tasks"}
        if tasks is not None and (not isinstance(tasks, list) or not tasks or any(task not in TASKS for task in tasks)):
            raise ValueError("tasks : liste non vide de tâches connues attendue.")
        # Une erreur locale ne doit pas faire oublier les autres tâches annoncées.
        if tasks is not None:
            self.scope_declared = True
            for task in tasks:
                self.event_data["tasks"].setdefault(task, {"status": "pending"})
        if not isinstance(requirements, dict) or set(requirements) - set(REQUIREMENT_PROPERTIES):
            raise ValueError("Champs d'exigences inconnus.")
        requirements = normalize_arguments(requirements, REQUIREMENT_PROPERTIES)
        if "preferences" in requirements:
            requirements["preferences"] = normalize_preferences(requirements["preferences"])
        if isinstance(requirements.get("event_type"), str):
            requirements["event_type"] = tools._normalize(requirements["event_type"]).replace(" ", "_")
        for field, filters in requirements.items():
            if field.endswith("_filters") and isinstance(filters, dict):
                filters = normalize_arguments(filters, CATALOGS[field[:-8]][2]["function"]["parameters"]["properties"])
                requirements[field] = filters
                if isinstance(filters.get("event_type"), str):
                    filters["event_type"] = tools._normalize(filters["event_type"]).replace(" ", "_")
        problem = event_type_error(requirements.get("event_type"), self.turn_request if self.interactive else self.event_data["request"])
        if problem:
            raise ValueError(problem)
        previous = self.event_data["requirements"]
        requirements = {key: value.strip() if isinstance(value, str) else value
                        for key, value in deepcopy(requirements).items()
                        if value is not None and not (REQUIREMENT_PROPERTIES[key]["type"] == "string"
                                                     and isinstance(value, str) and not value.strip())}
        for key, value in requirements.items():
            if (key in ("city", "province") and key in previous and isinstance(value, str)
                    and tools._normalize(previous[key]) == tools._normalize(value)):
                requirements[key] = value = previous[key]
            if key.endswith("_filters") and isinstance(value, dict) and isinstance(previous.get(key), dict):
                if any(field in previous[key] and previous[key][field] != content for field, content in value.items()):
                    raise ConstraintRevisionRequired(f"Contrainte déjà enregistrée : {key}. Utilisez revise_event.")
                requirements[key] = value = {**previous[key], **value}
                continue
            if key in previous and previous[key] != value and key != "preferences":
                raise ConstraintRevisionRequired(f"Contrainte déjà enregistrée : {key}. Utilisez revise_event.")
        requirements = {**previous, **requirements}
        for key, value in requirements.items():
            kind = REQUIREMENT_PROPERTIES[key]["type"]
            if kind == "string" and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{key} doit être une chaîne non vide.")
            if kind == "boolean" and type(value) is not bool:
                raise ValueError(f"{key} doit être booléen.")
            if "enum" in REQUIREMENT_PROPERTIES[key] and value not in REQUIREMENT_PROPERTIES[key]["enum"]:
                raise ValueError(f"Valeur invalide pour {key}.")
        guests, total = requirements.get("guests"), requirements.get("budget_total")
        for field in FINANCIAL_FIELDS:
            value = requirements.get(field)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError(f"Valeur financière invalide : {field}.")
        if "provider_vehicles" in requirements and (type(requirements["provider_vehicles"]) is not int or requirements["provider_vehicles"] < 1):
            raise ValueError("provider_vehicles : entier positif attendu.")
        if requirements.get("provider_vat_rate", 0) > 1:
            raise ValueError("provider_vat_rate : fraction entre 0 et 1 attendue, jamais un taux supposé.")
        if guests is not None and (type(guests) is not int or guests < 1):
            raise ValueError("Effectif invalide.")
        if total is not None and (type(total) not in (int, float) or not math.isfinite(total) or total < 0):
            raise ValueError("Budget total invalide.")
        for category in CATALOGS:
            filters = requirements.get(category + "_filters", {})
            if not isinstance(filters, dict):
                raise ValueError("Filtres attendus sous forme d'objet.")
            filters = normalize_arguments(filters, CATALOGS[category][2]["function"]["parameters"]["properties"])
            cleaned, problem = tools._validate_filters(filters, CATALOGS[category][2])
            if problem:
                raise ValueError(problem)
            problem = event_type_error(cleaned.get("event_type"), self.turn_request if self.interactive else self.event_data["request"])
            if problem:
                raise ValueError(problem)
            # Garder ids=[] : c'est une contrainte impossible, pas une absence de filtre.
            requirements[category + "_filters"] = {**cleaned, **({"ids": []} if filters.get("ids") == [] else {})}
            if not requirements[category + "_filters"]:
                requirements.pop(category + "_filters")
            for key in ("guests", "event_type", "rental_period"):
                if key in cleaned and key in requirements and cleaned[key] != requirements[key]:
                    raise ValueError(f"Exigences contradictoires pour {key}.")
        if "date" in requirements:
            if tools._parse_date(requirements["date"]) is None:
                raise ValueError("Date imposée invalide : format YYYY-MM-DD attendu.")
        if "planning" in requirements:
            start, _ = check_schedule(requirements["planning"])
            if start.date().isoformat() < self.today:
                raise TaskImpossible(f"Planning imposé passé ; date actuelle : {self.today}.")
            self.validate_planning(encode(requirements["planning"]), requirements)
        if requirements.get("document_kind", "synthesis") not in ("email", "synthesis"):
            raise ValueError("Type de document inconnu.")
        # Vérifier les décisions déjà acquises si de nouvelles contraintes arrivent.
        self.event_data["requirements"] = requirements
        try:
            if self.event_data["planning"]:
                self.validate_planning(encode(self.event_data["planning"]))
            self.check_selections()
        except (ValueError, TypeError, KeyError):
            self.event_data["requirements"] = previous
            raise
        if self.interactive and any(previous.get(key) != requirements.get(key)
                                    for key in set(previous) | set(requirements) if key != "document_kind"):
            self.material_change("Nouvelles contraintes enregistrées")
        if total is not None and any(category in self.event_data["tasks"] for category in CATALOGS):
            self.event_data["tasks"].setdefault("budget", {"status": "pending"})
        if requirements.get("document_kind"):
            self.event_data["tasks"].setdefault("documents", {"status": "pending"})
        if "planning" in requirements:
            self.event_data["planning"] = deepcopy(requirements["planning"])
            self.event_data["tasks"]["planning"] = {"status": "success"}

    def check_selections(self):
        """Ne pas certifier une décision devenue incompatible avec une contrainte tardive."""
        for category in CATALOGS:
            row = self.selected_row(category)
            if not row:
                continue
            context = self.context(category)
            if "ids" in context["filters"] and row["id"] not in context["filters"]["ids"]:
                raise ValueError(f"Identifiant {category} déjà choisi incompatible avec la nouvelle contrainte.")
            found = search_candidates(category, {**context["filters"], "ids": [row["id"]]},
                                      catalogs=self.catalogs, context=context)
            if not found["candidates"]:
                raise ValueError(f"La nouvelle contrainte est incompatible avec {category} déjà choisi.")
            if category != "venue":
                field, offer_key = ("formules", "formula_id") if category == "catering" else ("kits", "kit_id")
                selection = self.event_data["catering"] if category == "catering" else self.event_data["providers"][0]
                if not any(offer["id"] == selection[offer_key] for offer in found["candidates"][0][field]):
                    raise ValueError(f"Offre {category} déjà choisie incompatible avec la nouvelle contrainte.")

    def validate_planning(self, text, requirements=None):
        req = requirements if requirements is not None else self.event_data["requirements"]
        planning = parse_object(text)
        start, _ = check_schedule(planning)
        if start.date().isoformat() < self.today:
            raise ValueError("La date proposée est passée.")
        if not req.get("date") and not req.get("planning") and start.date().isoformat() == self.today:
            raise ValueError("Proposez une date future.")
        if req.get("weekday") and start.weekday() != tools.DAYS[req["weekday"]]:
            raise ValueError("Le jour de semaine imposé doit être conservé ; utilisez get_next_day.")
        if req.get("date") and start.date().isoformat() != req["date"]:
            raise ValueError("La date imposée doit être conservée.")
        if req.get("planning") and planning != req["planning"]:
            raise ValueError("Les horaires imposés doivent être conservés.")
        for category in (() if self.interactive else ("venue", "catering")):
            row = self.selected_row(category)
            if row and not compatible(category, row, {**self.context(category), "planning": planning}):
                raise ValueError(f"Horaires incompatibles avec {category} déjà choisi.")

    def validate_budget(self, text):
        budget = parse_object(text)
        if set(budget) != set(CATALOGS):
            raise ValueError("Le budget exige venue, catering et provider.")
        for category, value in budget.items():
            if (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
                    or not math.isclose(value * 20, round(value * 20), abs_tol=1e-8)):
                raise ValueError("Proportions finies entre 0 et 1, multiples de 0.05 attendues.")
            if self.scope_declared and category not in self.event_data["tasks"] and value != 0:
                raise ValueError(f"La catégorie {category} n'est pas demandée.")
        expected = 1 if not self.scope_declared or any(category in self.event_data["tasks"] for category in CATALOGS) else 0
        if not math.isclose(sum(budget.values()), expected, abs_tol=1e-8):
            raise ValueError(f"La somme des proportions doit valoir {expected}.")
        if self.interactive:
            return  # record réévalue les choix avec la nouvelle allocation validée.
        previous = self.event_data["budget"]
        self.event_data["budget"] = budget
        try:
            self.check_selections()
        finally:
            self.event_data["budget"] = previous

    def selected_row(self, category):
        if category == "provider":
            selection = self.event_data["providers"][0] if self.event_data["providers"] else None
        else:
            selection = self.event_data[category]
        if selection:
            return next(row for row in self.catalogs[category] if row["id"] == selection["id"])
        return None

    def context(self, task):
        req = self.event_data["requirements"]
        if task in ("planning", "budget"):
            fields = ("date", "weekday", "planning", "city", "event_type", "music_end", "preferences", "assumptions")
            if task == "budget":
                fields = ("budget_total", "guests", "preferences")
            result = {"requirements": {key: req[key] for key in fields if key in req}, "today": self.today,
                    "current_request": self.turn_request,
                    "request": self.event_data["request"],
                    "categories": [key for key in CATALOGS if key in self.event_data["tasks"]] if self.scope_declared else None}
            if task == "planning" and self.selected_row("venue"):
                result["venue_constraints"] = self.selected_row("venue")["contraintes"]
            if task == "planning":
                result["previous_planning"] = next((item["result"] for item in reversed(self.event_data["decision_archive"])
                                                     if item["task"] == "planning"), None)
            return result
        if task == "documents":
            context = self.consolidated()
            context["request"] = self.event_data["request"]
            context["current_request"] = self.turn_request
            for field in ("document_archive", "decision_archive", "approval_archive"):
                context.pop(field, None)
            context["contact_note"] = "Aucune coordonnée de contact dans le catalogue ; ne pas inventer d'adresse email. Brouillon uniquement."
            context.pop("documents")
            context["tasks"].pop("documents", None)
            return context
        filters = {key: value for key, value in req.get(task + "_filters", {}).items() if value is not None}
        for key, value in self.search_filters.get(task, {}).items():
            if key == "max_budget":
                filters[key] = min(filters.get(key, value), value)
            elif key in filters and filters[key] != value and key != "limit":
                raise ConstraintRevisionRequired(f"Filtre persistant {task}.{key} : utilisez revise_event.")
            else:
                filters[key] = value
        for field in ("guests", "event_type") if task != "provider" else ():
            if field in req:
                filters[field] = req[field]
        if task == "venue":
            if req.get("rental_period"):
                filters.setdefault("rental_period", req["rental_period"])
            for field in ("city", "province"):
                if field == "city" and self.event_data["location_search"] and self.event_data["location_search"]["expanded"]:
                    continue  # La ville souhaitée reste dans requirements ; les filtres effectifs sont provinciaux.
                if field in req:
                    if field in filters and tools._normalize(filters[field]) != tools._normalize(req[field]):
                        raise ValueError(f"Lieu contradictoire : {field}.")
                    filters[field] = req[field]
            if self.event_data["location_search"] and self.event_data["location_search"]["expanded"]:
                filters.setdefault("province", self.event_data["location_search"]["province"])
        venue = self.selected_row("venue")
        if task == "catering" and not venue:
            province = req.get("province")
            if not province and req.get("city") and isinstance(self.catalogs["venue"], list):
                provinces = {row["lieux"]["province"] for row in self.catalogs["venue"]
                             if tools._normalize(row["lieux"]["commune"]) == tools._normalize(req["city"])}
                if len(provinces) == 1:
                    province = provinces.pop()
            if province:
                # La commune du traiteur reste un filtre de siège, distinct de sa zone d'intervention.
                filters.setdefault("province", province)
        total = req.get("budget_total")
        if total is not None and self.event_data["budget"]:
            allocated = round(total * self.event_data["budget"][task], 2)
            filters["max_budget"] = min(filters.get("max_budget", allocated), allocated)
        weather = self.relevant_weather(filters.get("city")) if task == "venue" else None
        return {"filters": filters, "planning": self.event_data["planning"], "date": req.get("date"),
                **({"weather": weather} if weather else {}),
                "financial": {key: req[key] for key in FINANCIAL_FIELDS if key.startswith(task + "_") and key in req},
                "request": self.turn_request if self.interactive else self.event_data["request"],
                "current_request": self.turn_request,
                "venue": venue if task != "venue" else None, "today": self.today,
                "music_required": req.get("music_required", True), "music_end": req.get("music_end"),
                "space_type": req.get("venue_filters", {}).get("space_type"),
                "preferences": req.get("preferences", ""), "assumptions": req.get("assumptions", "")}

    def services_available(self, venue):
        """Écarter une impasse objective sans choisir les fournisseurs à la place du LLM."""
        if venue["id"] in self.event_data["rejected_venues"]:
            return False
        for category in ("catering", "provider"):
            if category not in self.event_data["tasks"]:
                continue
            context = self.context(category)
            context["venue"] = venue
            selected = self.selected_row(category)
            if selected:
                if not compatible(category, selected, context):
                    if self.event_data["venue_replacement_pending"]:
                        continue  # Le nouveau choix de salle fera réévaluer ce fournisseur seulement.
                    return False
                continue
            result = search_candidates(category, {**context["filters"], "limit": 1},
                                       catalogs=self.catalogs, context=context)
            if result["status"] in ("invalid_filters", "error"):
                # Un effectif manquant ou un catalogue indisponible ne condamne pas la salle.
                continue
            if result["status"] == "no_match":
                return False
        return True

    def search_options(self, task, filters, context, accept=None):
        result = search_candidates(task, filters, catalogs=self.catalogs, context=context, accept=accept)
        req = self.event_data["requirements"]
        if (task != "venue" or result["status"] != "no_match"
                or req.get("location_scope") != "province_fallback" or not filters.get("city")):
            return result
        # Les catalogues ne décrivent pas le voisinage : même province, jamais un rayon inventé.
        provinces = {row["lieux"]["province"] for row in self.catalogs["venue"]
                     if tools._normalize(row["lieux"]["commune"]) == tools._normalize(filters["city"])}
        if len(provinces) != 1:
            return result
        province = provinces.pop()
        if filters.get("province") and tools._normalize(filters["province"]) != tools._normalize(province):
            return result
        expanded = {key: value for key, value in filters.items() if key != "city"}
        expanded["province"] = province
        self.event_data["location_search"] = {
            "requested_city": filters["city"], "expanded": True, "province": province,
            "basis": "Même province du catalogue ; proximité et temps de trajet non établis."}
        return search_candidates(task, expanded, catalogs=self.catalogs, context=context, accept=accept)

    def delegate(self, agent, demande, task, *, requirements=None, tasks=None):
        self.turn_actions = True
        if task not in TASKS:
            return {"status": "error", "error": "Catégorie de tâche inconnue."}
        if self.event_data["pending_input"] and self.interactive:
            return {"status": "waiting", "task": task, "reason": "Réponse humaine attendue."}
        if self.plan_refused() and (not self.interactive or task == "documents"):
            return {"status": "reused", "task": task, "outcome": "refused",
                    "reason": "Plan global refusé ; génération des emails arrêtée."}
        before = deepcopy(self.event_data) if self.interactive else None
        previous_scope = self.scope_declared
        previous_attempts = deepcopy(self.attempts)
        failed = self.failed_contexts() if self.interactive else {}
        previous_filters = deepcopy(self.search_filters)
        try:
            if self.interactive:
                scoped = self.scoped_budget(requirements)
                if task == "budget" and scoped != requirements:
                    return {"status": "not_required", "reason": "Plafond du prestataire uniquement ; budget global conservé."}
                requirements = scoped
                temporary = None
                if task in CATALOGS and isinstance(requirements, dict):
                    requirements = deepcopy(requirements)
                    temporary = requirements.pop(task + "_filters", None)
                    if temporary is not None:
                        temporary = normalize_arguments(temporary, CATALOGS[task][2]["function"]["parameters"]["properties"])
                        cleaned, problem = tools._validate_filters(temporary, CATALOGS[task][2])
                        if problem:
                            raise ValueError(problem)
                        cleaned = {**cleaned, **({"ids": []} if temporary.get("ids") == [] else {})}
                        persistent = {key: value for key, value in cleaned.items() if key not in ("max_budget", "ids", "limit")}
                        if persistent:
                            requirements[task + "_filters"] = persistent
                        temporary = {key: value for key, value in cleaned.items() if key in ("max_budget", "ids", "limit")}
            self.remember(requirements, tasks)
            if self.interactive:
                if temporary is not None:
                    self.search_filters[task] = temporary
                    self.context(task)  # Refuser toute relaxation implicite d'un filtre persistant.
                    if self.recheck_choices():
                        self.material_change("Sélection incompatible avec le nouveau filtre de recherche")
                self.reopen_changed_failures(failed, reset_attempts=False)
        except ConstraintRevisionRequired as error:
            if self.interactive:
                self.event_data.clear()
                self.event_data.update(before)
                self.search_filters = previous_filters
                self.scope_declared = previous_scope
                self.attempts = previous_attempts
                return {"status": "requires_revision", "task": task, "error": str(error)}
            reused = self.begin_attempt(task, {"invalid_requirements": requirements, "tasks": tasks})
            if reused:
                return reused
            return self.record(task, {"status": "error", "error": str(error), "code": "invalid_requirements"})
        except (ValueError, TypeError, KeyError, TaskImpossible) as error:
            reused = self.begin_attempt(task, {"invalid_requirements": requirements, "tasks": tasks})
            if reused:
                return reused
            return self.record(task, {"status": "error", "error": str(error), "code": "invalid_requirements"})
        self.event_data["tasks"].setdefault(task, {"status": "pending"})
        if self.interactive and task == "documents" and self.event_data["tasks"][task]["status"] == "skipped":
            self.event_data["tasks"][task] = {"status": "pending"}
        if (task == "documents" and self.event_data["requirements"].get("document_kind") == "email"
                and "venue" in self.event_data["tasks"] and not self.event_data["venue"]):
            venue_status = self.event_data["tasks"]["venue"]["status"]
            if venue_status == "pending":
                return {"status": "not_ready", "task": task, "requires": "venue"}
            reused = self.begin_attempt(task, {"venue": None})
            if reused:
                return reused
            return self.record(task, {"status": "blocked", "error": "Email bloqué : aucune salle sélectionnée ; destinataire inconnu."})
        is_email = task == "documents" and self.event_data["requirements"].get("document_kind") == "email"
        if is_email:
            pending = [name for name, outcome in self.event_data["tasks"].items()
                       if name != "documents" and outcome["status"] == "pending"]
            if pending:
                return {"status": "not_ready", "task": task, "requires": pending[0]}
        if task == "budget" and self.event_data["requirements"].get("budget_total") is None:
            return self.record(task, {"status": "skipped", "reason": "Aucun plafond connu : répartition inutile."})
        # Seul un plafond connu exige une répartition avant les achats multiples.
        if task in CATALOGS and self.event_data["requirements"].get("budget_total") is not None and not self.event_data["budget"]:
            self.event_data["tasks"].setdefault("budget", {"status": "pending"})
            budget_status = self.event_data["tasks"].get("budget", {}).get("status", "pending")
            if budget_status != "pending":
                return self.record(task, {"status": "blocked", "error": "Répartition du budget indisponible ; plafond conservé."})
            return {"status": "not_ready", "task": task, "requires": "budget"}
        try:
            context = self.context(task)
        except (ValueError, TypeError, KeyError) as error:
            reused = self.begin_attempt(task, {"invalid_requirements": self.event_data["requirements"]})
            if reused:
                return reused
            return self.record(task, {"status": "error", "error": str(error), "code": "invalid_requirements"})
        # Ignorer les reformulations de demande ; comparer les paramètres structurés
        # et les décisions dont la tâche dépend réellement (date, budget, salle...).
        relevant = {key: value for key, value in context.items() if key not in ("request", "current_request", "today")}
        if relevant.get("venue"):
            relevant["venue"] = relevant["venue"]["id"]
        reused = self.begin_attempt(task, relevant)
        if reused:
            return reused
        if self.counters["delegations"] >= self.counters["max_delegations"]:
            return self.record(task, {"status": "limit", "error": "Limite globale de délégations atteinte."})
        if is_email:
            approved = self.approve_plan()
            if approved is None:
                self.event_data["pending_input"]["resume"] = {"demande": demande}
                return {"status": "waiting", "task": task}
            if not approved:
                return self.record(task, {"status": "refused",
                    "error": "Plan global refusé ; génération des emails arrêtée. Aucun email généré ni envoyé."})
            context = self.context(task)
        payload = {"task": task, "demande": demande, **context}
        original_functions, original_schemas = agent.fonctions, agent.schemas
        approval = {}
        if task in CATALOGS:
            accept = self.services_available if task == "venue" else None
            # Sans filtre explicite, le spécialiste extrait les critères de la demande
            # avec son outil avant de sélectionner ; pas de catalogue arbitraire injecté.
            result = (self.search_options(task, context["filters"], context, accept)
                      if context["filters"] or context.get("venue") else {"status": "needs_search", "candidates": []})
            if task == "venue" and self.event_data["location_search"]:
                context["filters"] = dict(result.get("filters", context["filters"]))
                payload["filters"] = context["filters"]
                payload["location_search"] = deepcopy(self.event_data["location_search"])
                payload.pop("weather", None)  # La prévision de la ville initiale ne vaut pas pour une autre commune.
            if result["status"] not in ("matched", "needs_search"):
                if task == "venue" and result["status"] == "no_match":
                    result["error"] = ("Aucune salle compatible avec les contraintes obligatoires ("
                                       + describe_filters(context["filters"]) + ") et les services demandés. Disponibilité non vérifiée.")
                outcome = self.record(task, {"status": result["status"], "error": result.get("error", "Recherche impossible.")})
                return outcome
            proposed = list(result["candidates"])
            selection_filters = dict(context["filters"])
            payload["search"] = result
            if payload.get("venue"):
                payload["venue"] = {key: payload["venue"][key] for key in ("id", "lieux", "equipements", "contraintes")}

            def search(**extra):
                extra = normalize_arguments(extra, CATALOGS[task][2]["function"]["parameters"]["properties"])
                for key, value in context["filters"].items():
                    if key != "limit" and key in extra and extra[key] != value:
                        return {"status": "invalid_filters", "error": f"Contrainte fixée : {key}."}
                requested = {**context["filters"], **extra}
                if result["candidates"] and {"limit": 5, **requested} == {"limit": 5, **context["filters"]}:
                    return {"status": "reused", "source": "search.candidates dans le contexte initial"}
                found = self.search_options(task, requested, context, accept)
                if task == "venue" and self.event_data["location_search"]:
                    found["location_search"] = deepcopy(self.event_data["location_search"])
                # Une recherche corrigée remplace les anciens candidats : ils peuvent
                # ne plus respecter le type, la capacité ou les exclusions précisés.
                proposed[:] = found["candidates"]
                selection_filters.clear()
                selection_filters.update(found.get("filters", requested))
                return found

            _, schema = compact_tool(task)
            agent.fonctions, agent.schemas = {schema["function"]["name"]: search}, [schema]

            def validate(text):
                selected = parse_object(text)
                offer_key = {"catering": "formula_id", "provider": "kit_id"}.get(task)
                if set(selected) != ({"id", offer_key} if offer_key else {"id"}):
                    raise ValueError("Retournez uniquement id et, si nécessaire, formula_id ou kit_id.")
                candidates = [row for row in proposed if type(selected["id"]) is type(row["id"]) and row["id"] == selected["id"]]
                if not candidates:
                    raise ValueError("Identifiant absent des candidats compatibles proposés.")
                if offer_key:
                    offers = [offer for row in candidates for offer in row["formules" if task == "catering" else "kits"]]
                    if not any(type(offer["id"]) is type(selected[offer_key]) and offer["id"] == selected[offer_key] for offer in offers):
                        raise ValueError("Offre absente des offres compatibles proposées ou type d'identifiant incorrect.")
        elif task == "planning":
            validate = self.validate_planning
            agent.fonctions = {**original_functions, "get_meteo": self.observe_weather}
        elif task == "budget":
            validate = self.validate_budget
        else:
            def approve(message):
                if self.interactive:
                    if not isinstance(message, str) or not message.strip():
                        raise ValueError("Le brouillon doit être un texte non vide.")
                    approval["content"] = message
                    self.event_data["pending_input"] = {"kind": "email_approval", "content": message,
                                                        "revision": self.event_data["revision"]}
                    return {"status": "waiting", "reason": "Validation du brouillon au prochain message."}
                if approval:
                    return {"approved": approval["approved"], "already_asked": True}
                approval.update(tools.ask_user(message), content=message)
                return {"approved": approval["approved"]}

            agent.fonctions, agent.schemas = {"ask_user": approve}, [tools.SCHEMA_ASK_USER]

            def validate(text):
                if self.event_data["requirements"].get("document_kind") == "email":
                    if not approval:
                        raise ValueError("Présentez le brouillon complet avec ask_user avant de conclure.")
                    if text != approval["content"]:
                        raise ValueError("Le texte final doit être exactement le brouillon approuvé.")
        self.counters["delegations"] += 1
        try:
            output = agent.run(encode(payload), counters=self.counters, validate=validate,
                               completed=(lambda: bool(approval)) if is_email else None,
                               completed_output=(lambda: approval.get("content")) if task == "documents" else None,
                               compact_output=task in CATALOGS and bool(proposed))
            if task == "documents" and self.interactive and self.event_data["pending_input"]:
                outcome = {"status": "waiting"}
            elif task == "documents" and approval:
                outcome = {"status": "success" if approval["approved"] else "refused",
                           "result": {"content": approval["content"], "approved": approval["approved"]}}
                if not approval["approved"]:
                    outcome["error"] = "Brouillon d'email refusé ; aucun envoi."
            elif agent.last_status != "success":
                outcome = {"status": "error", "error": getattr(agent, "last_reason", "Aucun résultat valide."),
                           "code": agent.last_error_code or "incomplete"}
            else:
                validate(output)
                outcome = {"status": "success", "result": ({"content": output, "approved": approval.get("approved")}
                           if task == "documents" else parse_object(output))}
        except Exception as error:
            outcome = {"status": "error", "error": str(error),
                       "code": "impossible" if isinstance(error, TaskImpossible) else "execution_error"}
        finally:
            agent.fonctions, agent.schemas = original_functions, original_schemas
        if task in CATALOGS and outcome["status"] == "success":
            # Conserver les paramètres réellement utilisés pour calculer le choix validé.
            if self.interactive:
                self.event_data["selection_filters"][task] = {key: value for key, value in selection_filters.items() if key != "limit"}
                # Conserver les critères extraits par le spécialiste, sans figer les plafonds
                # calculés ni les contraintes de la salle dans les exigences des tours suivants.
                discovered = {key: value for key, value in selection_filters.items()
                              if key not in ("limit", "max_budget", "ids") and key not in context["filters"]}
                if discovered:
                    field = task + "_filters"
                    self.event_data["requirements"][field] = {**self.event_data["requirements"].get(field, {}), **discovered}
            else:
                self.event_data["requirements"][task + "_filters"] = {
                    key: value for key, value in selection_filters.items() if key != "limit"}
        outcome = self.record(task, outcome)
        return outcome

    def begin_attempt(self, task, relevant):
        """Deux tentatives au plus, la seconde seulement si le contexte utile change."""
        previous = self.event_data["tasks"].get(task, {})
        attempt = self.attempts.get(task, {"count": 0, "input": None})
        if previous.get("status") not in (None, "pending"):
            reason = None
            if not previous.get("retryable", False):
                reason = "Résultat validé ou erreur permanente pour cette exécution."
            elif attempt["count"] >= 2:
                reason = "Limite de deux tentatives atteinte."
            elif relevant == attempt["input"]:
                reason = "Paramètres pertinents inchangés ; aucune nouvelle tentative."
            if reason:
                return {"status": "reused", "task": task, "outcome": previous["status"],
                        "retryable": previous.get("retryable", False), "code": previous.get("code"),
                        "error_kind": previous.get("error_kind"),
                        "reason": reason, **({"error": previous["error"]} if "error" in previous else {})}
        self.attempts[task] = {"count": attempt["count"] + 1, "input": deepcopy(relevant)}
        self.event_data["tasks"][task] = {"status": "pending"}
        return None

    def observe_weather(self, day, city):
        """Conserver le résultat de l'outil actuel, une seule consultation par ville/date/run."""
        city = city.strip() if isinstance(city, str) else city
        for observation in self.event_data["weather"]:
            if observation["date"] == day and str(observation["city"]).casefold() == str(city).casefold():
                return deepcopy(observation)
        result = tools.get_meteo(day=day, city=city)
        observation = {"city": city, "date": day, "source": "Open-Meteo"}
        if "error" in result:
            observation.update(status="unavailable", code=result.get("code"), error=result["error"])
        else:
            observation.update(status="available", resolved_city=result["city"], country=result.get("country"),
                               forecast={key: result[key] for key in tools.WEATHER_FIELDS})
        self.event_data["weather"].append(observation)
        return deepcopy(observation)

    def relevant_weather(self, city):
        planning = self.event_data["planning"]
        day = planning["start"][:10] if planning else self.event_data["requirements"].get("date")
        return next((deepcopy(item) for item in self.event_data["weather"]
                     if day and city and item["date"] == day and isinstance(item["city"], str)
                     and tools._normalize(item["city"]) == tools._normalize(city)), None)

    def can_repair(self):
        if self.event_data["pending_input"]:
            return False
        if self.plan_refused():
            return False
        return any(outcome.get("retryable", False) and self.attempts.get(task, {}).get("count", 0) < 2
                   for task, outcome in self.event_data["tasks"].items())

    def record(self, task, outcome):
        failed = self.failed_contexts() if self.interactive else {}
        field = {"provider": "providers", "documents": "documents"}.get(task, task)
        if self.interactive and outcome["status"] == "success" and task != "documents":
            value = [outcome["result"]] if field == "providers" else outcome["result"]
            if self.event_data[field] != value:
                self.material_change("Décision actualisée : " + task)
        if outcome.get("code") in ("invalid_output", "interrupted_output"):
            outcome = {**outcome, "error_kind": "temporary_output"}
        elif outcome.get("code") == "impossible" or outcome["status"] == "no_match":
            outcome = {**outcome, "error_kind": "business_impossibility"}
        retryable = (outcome["status"] in ("error", "no_match", "invalid_filters", "blocked")
                     and outcome.get("code") != "llm_error")
        outcome = {"task": task, **outcome, "retryable": retryable and self.attempts.get(task, {}).get("count", 0) < 2}
        self.event_data["tasks"][task] = {key: value for key, value in outcome.items() if key != "result"}
        if outcome["status"] == "success":
            field = {"provider": "providers", "documents": "documents"}.get(task, task)
            self.event_data[field] = [outcome["result"]] if field in ("providers", "documents") else outcome["result"]
            if task in CATALOGS:
                outcome["details"] = self.selection_details(task)
            if self.interactive and task in ("planning", "budget", "venue"):
                self.recheck_choices()
                if task == "venue":
                    self.event_data["venue_replacement_pending"] = False
                self.reopen_changed_failures(failed)
        elif task == "documents" and outcome["status"] == "refused" and "result" in outcome:
            self.event_data["documents"] = [outcome["result"]]
        return outcome

    def material_change(self, reason):
        """Les accords et textes restent attachés à leur ancienne version, jamais réutilisés."""
        revision = self.event_data["revision"]
        approval = self.event_data["plan_approval"]
        if approval:
            self.event_data["approval_archive"].append({**deepcopy(approval), "revision": approval.get("revision", revision),
                                                       "reason": reason})
        for document in self.event_data["documents"]:
            self.event_data["document_archive"].append({**deepcopy(document), "revision": document.get("revision", revision)})
        pending = self.event_data["pending_input"]
        if pending and pending["kind"] == "email_approval":
            self.event_data["document_archive"].append({"content": pending["content"], "approved": None, "revision": revision})
        self.event_data["revision"] += 1
        self.event_data["plan_approval"] = None
        self.event_data["documents"] = []
        self.event_data["pending_input"] = None
        if "documents" in self.event_data["tasks"] and self.event_data["tasks"]["documents"]["status"] != "pending":
            self.event_data["tasks"]["documents"] = {"status": "skipped", "reason": "Nouvelle rédaction à demander après modification du plan."}

    def invalidate(self, task, reason, *, remove=False):
        field = {"provider": "providers", "documents": "documents"}.get(task, task)
        previous = self.event_data[field]
        if previous and task != "documents":
            self.event_data["decision_archive"].append({"task": task, "result": deepcopy(previous),
                                                        "revision": self.event_data["revision"], "reason": reason})
        self.event_data[field] = [] if field in ("providers", "documents") else None
        self.event_data["selection_filters"].pop(task, None)
        self.attempts.pop(task, None)
        if remove:
            self.event_data["tasks"].pop(task, None)
        else:
            self.event_data["tasks"][task] = {"status": "pending", "error": reason}

    def recheck_choices(self):
        """Capacité, horaires, offres, plafonds et fournisseurs imposés, sans nouveau choix Python."""
        invalidated = []
        for category in CATALOGS:
            field = "providers" if category == "provider" else category
            selection = (self.event_data[field][0] if self.event_data[field] else None) if category == "provider" else self.event_data[field]
            if not selection:
                continue
            context = self.context(category)
            found = search_candidates(category, {**context["filters"], "ids": [selection["id"]]},
                                      catalogs=self.catalogs, context=context)
            allowed_ids = context["filters"].get("ids")
            valid = bool(found["candidates"]) and (allowed_ids is None or selection["id"] in allowed_ids)
            if valid and category != "venue":
                field, key = ("formules", "formula_id") if category == "catering" else ("kits", "kit_id")
                valid = any(offer["id"] == selection[key] for row in found["candidates"] for offer in row[field])
            if not valid:
                self.invalidate(category, "Choix à réévaluer après modification des contraintes.")
                invalidated.append(category)
        return invalidated

    def revise_event(self, requirements=None, replace=None, remove=None, unset=None):
        """Appliquer une modification explicite du Supervisor ; transaction locale et contrôles existants."""
        requirements = self.scoped_budget({} if requirements is None else requirements)
        replace, remove, unset = ([] if value is None else value for value in (replace, remove, unset))
        if not isinstance(requirements, dict) or set(requirements) - set(REQUIREMENT_PROPERTIES):
            raise ValueError("Champs d'exigences inconnus.")
        for values, allowed in ((replace, TASKS), (remove, ("venue", "catering", "provider", "documents")), (unset, REQUIREMENT_PROPERTIES)):
            if not isinstance(values, list) or any(value not in allowed for value in values):
                raise ValueError("Modification de tâche ou contrainte inconnue.")
        if set(replace) & set(remove):
            raise ValueError("Une tâche ne peut être remplacée et supprimée simultanément.")
        before = deepcopy(self.event_data)
        attempts = deepcopy(self.attempts)
        scope = self.scope_declared
        actions = self.turn_actions
        failed = self.failed_contexts()
        previous_filters = deepcopy(self.search_filters)
        candidate = deepcopy(before["requirements"])
        for key in unset:
            candidate.pop(key, None)
        for key, value in requirements.items():
            if value is None:
                candidate.pop(key, None)
            elif key.endswith("_filters") and isinstance(value, dict):
                candidate[key] = {**candidate.get(key, {}), **value}
            else:
                candidate[key] = value
        schedule_changed = bool(set(requirements) & {"date", "weekday", "planning"} or set(unset) & {"date", "weekday", "planning"} or "planning" in replace)
        if schedule_changed and "planning" not in requirements:
            candidate.pop("planning", None)
            if "weekday" in requirements or "planning" in replace:
                candidate.pop("date", None)
        for key in ("guests", "event_type", "rental_period"):
            if key in requirements or key in unset:
                for category in CATALOGS:
                    filters = candidate.get(category + "_filters", {})
                    if key in filters:
                        if key in candidate:
                            filters[key] = candidate[key]
                        else:
                            filters.pop(key)
        geographic_change = bool((set(requirements) | set(unset)) & {"city", "province", "location_scope"})
        if geographic_change:
            for key in ("city", "province"):
                candidate.get("venue_filters", {}).pop(key, None)
        if "venue" in replace and before["venue"]:
            filters = candidate.get("venue_filters", {})
            if "ids" in filters:
                remaining = [value for value in filters["ids"] if value != before["venue"]["id"]]
                if remaining:
                    filters["ids"] = remaining
                else:
                    filters.pop("ids")
        for task in remove:
            candidate.pop("document_kind" if task == "documents" else task + "_filters", None)
        try:
            # Valider les nouvelles exigences sans laisser d'anciennes décisions bloquer leur mise à jour.
            original = self.event_data
            self.event_data = {**deepcopy(before), "requirements": {}, "planning": None, "venue": None,
                               "catering": None, "providers": [], "tasks": {}}
            try:
                self.remember(candidate)
                candidate = deepcopy(self.event_data["requirements"])
            finally:
                self.event_data = original
                self.scope_declared = scope
            if candidate == before["requirements"] and not replace and not remove:
                return {"status": "unchanged"}
            self.material_change("Modification demandée par l'utilisateur")
            self.turn_actions = True
            self.event_data["requirements"] = candidate
            # Une révision explicite remplace les essais antérieurs de cette catégorie.
            changed = {key for key in set(before["requirements"]) | set(candidate)
                       if before["requirements"].get(key) != candidate.get(key)}
            for category in CATALOGS:
                shared_change = changed & {"budget_total", "date", "weekday", "planning", "music_end", "music_required"}
                selection_change = category != "provider" and changed & {"city", "province", "location_scope", "guests", "event_type", "rental_period"}
                if category + "_filters" in changed or category in replace + remove or shared_change or selection_change:
                    self.search_filters.pop(category, None)
            if geographic_change:
                self.event_data["location_search"] = None
            if before["requirements"].get("budget_total") != candidate.get("budget_total"):
                self.invalidate("budget", "Budget modifié.", remove=candidate.get("budget_total") is None)
            if schedule_changed:
                self.invalidate("planning", "Date ou horaires à réévaluer.")
                if candidate.get("planning"):
                    self.event_data["planning"] = deepcopy(candidate["planning"])
                    self.event_data["tasks"]["planning"] = {"status": "success"}
            for task in replace + remove:
                if task == "venue":
                    self.event_data["venue_replacement_pending"] = task in replace
                    if before["venue"] and task in replace:
                        self.event_data["rejected_venues"].append(before["venue"]["id"])
                self.invalidate(task, "Nouvelle proposition demandée." if task in replace else "Prestation supprimée.", remove=task in remove)
            invalidated = self.recheck_choices()
            invalidated.extend(self.reopen_changed_failures(failed))
            return {"status": "updated", "revision": self.event_data["revision"], "invalidated": invalidated,
                    "tasks": deepcopy(self.event_data["tasks"])}
        except (ValueError, TypeError, KeyError, TaskImpossible):
            self.event_data.clear()
            self.event_data.update(before)
            self.attempts = attempts
            self.turn_actions = actions
            self.search_filters = previous_filters
            raise

    def scoped_budget(self, requirements):
        """Garde-fou de la trace : un montant explicitement demandé pour le seul prestataire est local."""
        if not self.interactive or not isinstance(requirements, dict) or requirements.get("budget_total") is None:
            return requirements
        if requirements["budget_total"] == self.event_data["requirements"].get("budget_total"):
            return requirements  # Répéter le plafond global courant ne le modifie pas.
        wording = tools._normalize(self.turn_request)
        if (not re.search(r"\bprestataires?\b", wording)
                or re.search(r"\b(?:global|total|evenement|salle|traiteur|ensemble)\b", wording)
                or re.search(r"\b(?:sans|pas de|aucun) prestataire", wording)):
            return requirements
        amounts = {float(re.sub(r"\s", "", value).replace(",", "."))
                   for value in re.findall(r"(\d(?:[\d\s]*\d)?(?:[.,]\d{1,2})?)\s*(?:euros?\b|eur\b|€)", wording)}
        if len(amounts) != 1 or requirements["budget_total"] not in amounts:
            raise ValueError("Plafond du prestataire ambigu : préciser provider_filters.max_budget sans modifier budget_total.")
        scoped = deepcopy(requirements)
        ceiling = scoped.pop("budget_total")
        filters = scoped.setdefault("provider_filters", {})
        if not isinstance(filters, dict) or filters.get("max_budget", ceiling) != ceiling:
            raise ValueError("Plafonds du prestataire contradictoires.")
        filters["max_budget"] = ceiling
        return scoped

    def failed_contexts(self):
        """Photographier les entrées utiles des seuls échecs susceptibles d'être réouverts."""
        contexts = {}
        for task, outcome in self.event_data["tasks"].items():
            if task not in TASKS or outcome["status"] not in ("error", "no_match", "invalid_filters", "blocked", "limit"):
                continue
            try:
                context = self.context(task)
            except (ValueError, TypeError, KeyError):
                context = {"invalid_context": True}
            context = {key: value for key, value in context.items() if key not in ("request", "current_request", "today")}
            if task in CATALOGS:
                context["budget_total"] = self.event_data["requirements"].get("budget_total")
                context["persistent_filters"] = self.event_data["requirements"].get(task + "_filters", {})
            if task == "venue":
                context["location_scope"] = self.event_data["requirements"].get("location_scope")
                context["services"] = {category: {"filters": self.context(category)["filters"],
                                                   "selection": self.event_data["providers" if category == "provider" else category]}
                                       for category in ("catering", "provider") if category in self.event_data["tasks"]}
            contexts[task] = deepcopy(context)
        return contexts

    def reopen_changed_failures(self, before, *, reset_attempts=True):
        reopened = []
        for task, context in self.failed_contexts().items():
            if task in before and context != before[task]:
                if reset_attempts:
                    self.invalidate(task, "Contraintes utiles modifiées ; nouvelle recherche autorisée.")
                else:
                    # Une correction de filtre garde le plafond de deux essais du tour.
                    self.event_data["tasks"][task]["retryable"] = True
                reopened.append(task)
        return reopened

    def selection_details(self, category):
        row = self.selected_row(category)
        if not row:
            return None
        details = {key: row[key] for key in ("id", "nom", "lieux")}
        if category == "venue":
            details.update({key: row[key] for key in ("prix", "equipements", "contraintes")})
        else:
            selection = self.event_data["catering"] if category == "catering" else self.event_data["providers"][0]
            offer_key, field = ("formula_id", "formules") if category == "catering" else ("kit_id", "kits")
            details["offer"] = next(offer for offer in row[field] if offer["id"] == selection[offer_key])
            for key in ("frais_supplementaires", "deplacement", "boissons", "reservation"):
                if key in row:
                    details[key] = row[key]
        return details

    def settled(self):
        tasks = self.event_data["tasks"]
        return bool(tasks) and all(value["status"] != "pending" for value in tasks.values())

    def complete(self):
        if self.interactive:
            return bool(self.event_data["pending_input"]) or (self.scope_declared and self.settled())
        # Sans liste annoncée, le Supervisor reste libre de déléguer une autre tâche.
        return self.plan_refused() or ((self.scope_declared or self.event_data["plan_approval"] is not None)
                                       and self.settled())

    def plan_refused(self):
        approval = self.event_data["plan_approval"]
        return approval is not None and approval["approved"] is False

    def approve_plan(self):
        """Présenter le bilan Python avant Writer ; l'accord porte sur ce plan précis."""
        plan = self.consolidated()
        for field in ("plan_approval", "documents", "pending_input", "document_archive", "decision_archive", "approval_archive"):
            plan.pop(field)
        plan["tasks"].pop("documents", None)
        previous = self.event_data["plan_approval"]
        if previous and previous["plan"] == plan:
            return previous["approved"]
        self.event_data["plan_approval"] = None
        content = ("Plan global proposé avant rédaction des emails\n\n"
                   + self.summary("Plan à valider avant rédaction des emails.")
                   + "\n\nApprouvez-vous ce plan global et autorisez-vous la génération des brouillons d'emails ?")
        if self.interactive:
            self.event_data["pending_input"] = {"kind": "plan_approval", "content": content, "plan": plan,
                                                "revision": self.event_data["revision"]}
            return None
        approved = tools.ask_user(content)["approved"]
        self.event_data["plan_approval"] = {"content": content, "plan": plan, "approved": approved}
        return approved

    def validate_final(self, text):
        if self.interactive and not self.turn_actions:
            self.direct_reply = text
            return
        if not self.settled():
            raise ValueError("Aucun résultat ou tâches encore incomplètes : poursuivez les délégations utiles.")

    def successful(self):
        if self.interactive and self.turn_actions and not self.event_data["tasks"]:
            return True
        return self.settled() and any(value["status"] == "success" for value in self.event_data["tasks"].values()) and all(
            value["status"] in ("success", "skipped") for value in self.event_data["tasks"].values())

    def costs(self):
        req, known, details, missing = self.event_data["requirements"], {}, {}, []
        deposit = 0
        for category in CATALOGS:
            row = self.selected_row(category)
            if not row:
                continue
            if category == "venue":
                period = self.context("venue")["filters"].get("rental_period")
                detail = venue_cost(row, tools._normalize(period) if period else None)
                known["venue_ttc"], deposit = detail["known_ttc"], detail["deposit"]
                if detail["period_assumed"]:
                    missing.append("Période de location à confirmer : tarif journée utilisé par défaut, tarif soirée également indiqué.")
            elif category == "catering":
                detail = catering_cost(row, self.selection_details(category)["offer"],
                                       self.context(category)["filters"].get("guests"), req.get("catering_distance_km"))
                if detail["known_ttc"] is not None:
                    known["catering_ttc"] = detail["known_ttc"]
                else:
                    missing.append("Effectif du repas à préciser.")
                if detail["travel_ttc"] is None:
                    missing.append("Kilométrage aller-retour du traiteur à confirmer, en supplément du forfait livraison.")
                missing.append("Boissons et éventuel personnel supplémentaire à chiffrer.")
            elif category == "provider":
                detail = provider_cost(row, self.selection_details(category)["offer"], req)
                known["provider_ht"] = detail["known_ht"]
                if detail["known_ttc"] is not None:
                    known["provider_ttc"] = detail["known_ttc"]
                else:
                    missing.append("Taux de TVA du prestataire à confirmer ; son montant HT est exclu du sous-total TTC.")
                if detail["travel_ht"] is None:
                    missing.append("Kilométrage facturable et nombre de véhicules du prestataire à confirmer.")
            details[category] = detail
        subtotal = sum((money(value) for key, value in known.items() if key.endswith("_ttc")), money(0))
        return {"known": known, "budget_total": req.get("budget_total"),
                "details": details, "deposit": deposit, "known_ttc_subtotal": amount(subtotal),
                "known_cash_required": amount(subtotal + money(deposit)), "missing": missing,
                "known_over_budget": req.get("budget_total") is not None and subtotal > money(req["budget_total"]),
                "allocations": ({key: amount(money(req["budget_total"]) * money(value)) for key, value in self.event_data["budget"].items()}
                                if req.get("budget_total") is not None and self.event_data["budget"] else None),
                "warning": "Coût total non confirmé : HT/TTC distincts, TVA prestataire, déplacements, boissons et personnel éventuels à chiffrer. Caution hors dépense."}

    def consolidated(self):
        data = deepcopy(self.event_data)
        data.pop("request")
        for category in CATALOGS:
            data[category] = self.selection_details(category)
        data.pop("providers")
        data["availability"] = "Disponibilités non vérifiées ; aucune réservation confirmée."
        data["costs"] = self.costs()
        return data

    def summary(self, reason):
        lines = ["Résultat " + ("consolidé." if self.successful() else "partiel. " + reason)]
        approval = self.event_data["plan_approval"]
        if approval:
            lines.append("Plan global approuvé." if approval["approved"] else
                         "Plan global refusé ; génération des emails arrêtée.")
        location = self.event_data["location_search"]
        if location:
            lines.append(f"Aucune salle compatible à {location['requested_city']} avec les critères recherchés ; "
                         f"recherche élargie à la province de {location['province']}. " + location["basis"])
        if self.event_data["planning"]:
            planning = self.event_data["planning"]
            start, end = (datetime.fromisoformat(planning[key]).strftime("%d/%m/%Y à %H:%M") for key in ("start", "end"))
            lines.append(f"Date proposée : du {start} au {end} (heure locale).")
        for category, label in (("venue", "Salle"), ("catering", "Traiteur"), ("provider", "Prestataire")):
            details = self.selection_details(category)
            if details:
                offer = details.get("offer")
                location = details["lieux"]
                location = location.get("commune", "") if isinstance(location, dict) else location
                lines.append(f"{label} : {details['nom']}" + (f" — {location}" if location else "") + ".")
                if offer:
                    name = offer.get("nom") or f"Kit {offer['id']}"
                    lines.append(f"  {name} : {offer.get('description', '').rstrip('.')}.")
        costs = self.costs()
        lines.extend(financial_lines(costs))
        for observation in self.event_data["weather"]:
            label = f"Météo consultée — {observation['city']}, {observation['date']} ({observation['source']}) : "
            if observation["status"] == "available":
                forecast = observation["forecast"]
                lines.append(label + f"{forecast['temperature_min']:g} à {forecast['temperature_max']:g} °C ; "
                             f"précipitations {forecast['precipitation_mm']:g} mm ; risque de pluie {forecast['rain_probability']:g} % ; "
                             f"vent maximal {forecast['wind_max_kmh']:g} km/h. Prévision, sans garantie des conditions réelles.")
            else:
                lines.append(label + "indisponible. " + observation["error"])
        labels = {"planning": "planning", "budget": "budget", "venue": "salle", "catering": "traiteur",
                  "provider": "prestataire", "documents": "documents"}
        for task, outcome in self.event_data["tasks"].items():
            if outcome["status"] not in ("success", "skipped"):
                unresolved = outcome.get("error", "tâche non terminée")
                if (task == "documents" and outcome["status"] == "pending"
                        and self.event_data["requirements"].get("document_kind") == "email"
                        and "venue" in self.event_data["tasks"] and not self.event_data["venue"]):
                    unresolved = "Email en attente : aucune salle sélectionnée ; destinataire inconnu"
                lines.append(f"Non résolu — {labels.get(task, task)} : {unresolved.rstrip('.')}.")
        for document in self.event_data["documents"]:
            label = ("Brouillon approuvé" if document.get("approved") is True else
                     "Brouillon refusé" if document.get("approved") is False else "Document")
            lines.append(label + " :\n" + document["content"])
        if self.event_data["document_archive"]:
            lines.append("Des brouillons de versions antérieures sont archivés ; leurs accords ne valent pas pour le plan actuel.")
        assumptions = self.event_data["requirements"].get("assumptions")
        if assumptions:
            lines.append("Hypothèses : " + assumptions)
        venue = self.selected_row("venue")
        if venue:
            constraints = venue["contraintes"]
            lines.append(f"Fin de la musique autorisée par la salle : {constraints['heure_fin_musique']}.")
            for category, flag, ids_key, label in (
                ("catering", "traiteur_impose", "traiteurs_autorises_ids", "Traiteurs autorisés par la salle"),
                ("provider", "prestataire_technique_impose", "prestataires_techniques_autorises_ids", "Prestataires techniques autorisés par la salle"),
            ):
                if constraints[flag]:
                    catalog = self.catalogs[category]
                    names = {row["id"]: row["nom"] for row in catalog} if isinstance(catalog, list) else {}
                    allowed = [names.get(identifier, f"référence {identifier} à vérifier") for identifier in constraints[ids_key]]
                    lines.append(label + " : " + (", ".join(allowed) or "aucun — contrainte non résolue") + ".")
        elif self.event_data["catering"] or self.event_data["providers"]:
            lines.append("Compatibilité du traiteur et des prestataires avec la future salle à confirmer.")
        lines.append("Disponibilités non vérifiées ; aucune réservation confirmée. Aucun email envoyé.")
        return "\n".join(lines)

    def turn_summary(self, reason):
        if self.event_data["pending_input"]:
            return self.event_data["pending_input"]["content"]
        if self.direct_reply is not None and not self.turn_actions:
            return self.direct_reply
        return self.summary(reason)

    def clarify(self, question):
        if not isinstance(question, str) or not question.strip():
            raise ValueError("Une question non vide est attendue.")
        if self.event_data["pending_input"]:
            return {"status": "waiting", "reason": "Une réponse est déjà attendue."}
        self.event_data["pending_input"] = {"kind": "clarification", "content": question}
        return {"status": "waiting"}

    def resolve_pending(self, text):
        """Oui/non valide des données exactes, sans modèle ; les autres messages restent au Supervisor."""
        pending = self.event_data["pending_input"]
        if not pending:
            return False
        if pending["kind"] == "clarification":
            self.answered_question = pending["content"]
            self.event_data["pending_input"] = None
            return False
        answer = text.strip().casefold().rstrip(".! ")
        if answer not in ("oui", "o", "yes", "y", "non", "n", "no"):
            return False
        if pending["revision"] != self.event_data["revision"]:
            raise ValueError("Cette validation appartient à une ancienne version du plan.")
        approved = answer in ("oui", "o", "yes", "y")
        self.event_data["pending_input"] = None
        self.turn_actions = True
        if pending["kind"] == "email_approval":
            self.record("documents", {"status": "success" if approved else "refused",
                **({"error": "Brouillon refusé ; aucun envoi."} if not approved else {}),
                "result": {"content": pending["content"], "approved": approved, "revision": pending["revision"]}})
        else:
            self.event_data["plan_approval"] = {key: pending[key] for key in ("content", "plan", "revision")}
            self.event_data["plan_approval"]["approved"] = approved
            if approved:
                self.event_data["tasks"]["documents"] = {"status": "pending"}
                self.resume_writer = pending["resume"]
            else:
                self.record("documents", {"status": "refused", "error": "Plan global refusé ; aucun email généré ni envoyé."})
        return True

    def conversation_context(self, messages):
        """Quelques échanges utiles et décisions courantes, jamais les historiques ReAct."""
        approval = self.event_data["plan_approval"]
        return {"initial_request": self.event_data["request"][:4000], "current_request": self.turn_request,
                "requirements": self.event_data["requirements"], "planning": self.event_data["planning"],
                "budget": self.event_data["budget"],
                "decisions": {category: self.selection_details(category) for category in CATALOGS},
                "costs": self.costs(), "tasks": self.event_data["tasks"],
                "pending_input": self.event_data["pending_input"],
                "answered_question": self.answered_question,
                "plan_approved": approval.get("approved") if approval else None,
                "documents": [{"approved": doc.get("approved"), "revision": doc.get("revision", 0)}
                              for doc in self.event_data["documents"]],
                "recent_messages": [{"role": item["role"], "content": item["content"][:1200]}
                                    for item in (messages or [])[-4:]]}
