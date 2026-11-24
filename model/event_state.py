"""Données et contrôles métier d'une demande. Aucun historique persistant."""

import json
import math
from copy import deepcopy
from datetime import date

from .agents import tools
from .agents.search import CATALOGS, check_schedule, compact_tool, load_catalogs, search_candidates, compatible


TASKS = ("planning", "budget", "venue", "catering", "provider", "documents")
REQUIREMENT_PROPERTIES = {
    "city": {"type": "string"}, "province": {"type": "string"},
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
    **{category + "_filters": schema["function"]["parameters"]
       for category, (_, _, schema, _) in CATALOGS.items()},
}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TaskImpossible(Exception):
    """Un spécialiste signale un conflit explicite : aucune réparation JSON à tenter."""


def parse_object(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Un objet JSON est attendu.")
    if isinstance(value.get("error"), str) and value["error"].strip():
        raise TaskImpossible(value["error"])
    return value


class EventRun:
    def __init__(self, request, max_llm_calls=24, max_delegations=8):
        self.event_data = {"request": request, "requirements": {}, "planning": None,
                           "budget": None, "venue": None, "catering": None,
                           "providers": [], "documents": [], "tasks": {}}
        self.catalogs = load_catalogs()
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
        if not isinstance(requirements, dict) or set(requirements) - set(REQUIREMENT_PROPERTIES):
            raise ValueError("Champs d'exigences inconnus.")
        if tasks is not None and (not isinstance(tasks, list) or not tasks or any(task not in TASKS for task in tasks)):
            raise ValueError("tasks : liste non vide de tâches connues attendue.")
        previous = self.event_data["requirements"]
        requirements = {key: value.strip() if isinstance(value, str) else value
                        for key, value in deepcopy(requirements).items()
                        if value is not None and not (REQUIREMENT_PROPERTIES[key]["type"] == "string"
                                                     and isinstance(value, str) and not value.strip())}
        for key, value in requirements.items():
            if key in previous and previous[key] != value:
                raise ValueError(f"Contrainte déjà enregistrée : {key}.")
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
        if guests is not None and (type(guests) is not int or guests < 1):
            raise ValueError("Effectif invalide.")
        if total is not None and (type(total) not in (int, float) or not math.isfinite(total) or total < 0):
            raise ValueError("Budget total invalide.")
        for category in CATALOGS:
            filters = requirements.get(category + "_filters", {})
            if not isinstance(filters, dict):
                raise ValueError("Filtres attendus sous forme d'objet.")
            cleaned, problem = tools._validate_filters(filters, CATALOGS[category][2])
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
        if tasks is not None:
            self.scope_declared = True
            for task in tasks:
                self.event_data["tasks"].setdefault(task, {"status": "pending"})
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
        for category in ("venue", "catering"):
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
                    "request": self.event_data["request"],
                    "categories": [key for key in CATALOGS if key in self.event_data["tasks"]] if self.scope_declared else None}
            if task == "planning" and self.selected_row("venue"):
                result["venue_constraints"] = self.selected_row("venue")["contraintes"]
            return result
        if task == "documents":
            context = self.consolidated()
            context.pop("documents")
            context["tasks"].pop("documents", None)
            return context
        filters = {key: value for key, value in req.get(task + "_filters", {}).items() if value is not None}
        for field in ("guests", "event_type") if task != "provider" else ():
            if field in req:
                filters[field] = req[field]
        if task == "venue":
            if req.get("rental_period"):
                filters.setdefault("rental_period", req["rental_period"])
            for field in ("city", "province"):
                if field in req:
                    if field in filters and tools._normalize(filters[field]) != tools._normalize(req[field]):
                        raise ValueError(f"Lieu contradictoire : {field}.")
                    filters[field] = req[field]
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
        return {"filters": filters, "planning": self.event_data["planning"], "date": req.get("date"),
                "request": self.event_data["request"],
                "venue": venue if task != "venue" else None, "today": self.today,
                "music_required": req.get("music_required", True), "music_end": req.get("music_end"),
                "space_type": req.get("venue_filters", {}).get("space_type"),
                "preferences": req.get("preferences", ""), "assumptions": req.get("assumptions", "")}

    def services_available(self, venue):
        """Écarter une impasse objective sans choisir les fournisseurs à la place du LLM."""
        for category in ("catering", "provider"):
            if category not in self.event_data["tasks"]:
                continue
            context = self.context(category)
            context["venue"] = venue
            selected = self.selected_row(category)
            if selected:
                if not compatible(category, selected, context):
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

    def delegate(self, agent, demande, task, *, requirements=None, tasks=None):
        if task not in TASKS:
            return {"status": "error", "error": "Catégorie de tâche inconnue."}
        try:
            self.remember(requirements, tasks)
        except (ValueError, TypeError, KeyError, TaskImpossible) as error:
            return self.record(task, {"status": "error", "error": str(error), "code": "invalid_requirements"})
        previous = self.event_data["tasks"].get(task)
        if previous and previous["status"] != "pending":
            return {"status": "reused", "task": task, "outcome": previous["status"]}
        self.event_data["tasks"].setdefault(task, {"status": "pending"})
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
            return self.record(task, {"status": "error", "error": str(error), "code": "invalid_requirements"})
        if self.counters["delegations"] >= self.counters["max_delegations"]:
            return self.record(task, {"status": "limit", "error": "Limite globale de délégations atteinte."})
        payload = {"task": task, "demande": demande, **context}
        original_functions, original_schemas = agent.fonctions, agent.schemas
        approval = {}
        if task in CATALOGS:
            accept = self.services_available if task == "venue" else None
            # Sans filtre explicite, le spécialiste extrait les critères de la demande
            # avec son outil avant de sélectionner ; pas de catalogue arbitraire injecté.
            result = (search_candidates(task, context["filters"], catalogs=self.catalogs, context=context, accept=accept)
                      if context["filters"] or context.get("venue") else {"status": "needs_search", "candidates": []})
            if result["status"] not in ("matched", "needs_search"):
                if task == "venue" and result["status"] == "no_match":
                    result["error"] = "Aucune salle ne respecte les contraintes et la compatibilité des services demandés."
                outcome = self.record(task, {"status": result["status"], "error": result.get("error", "Recherche impossible.")})
                return outcome
            proposed = list(result["candidates"])
            payload["search"] = result
            if payload.get("venue"):
                payload["venue"] = {key: payload["venue"][key] for key in ("id", "lieux", "equipements", "contraintes")}

            def search(**extra):
                for key, value in context["filters"].items():
                    if key != "limit" and key in extra and extra[key] != value:
                        return {"status": "invalid_filters", "error": f"Contrainte fixée : {key}."}
                requested = {**context["filters"], **extra}
                if result["candidates"] and {"limit": 5, **requested} == {"limit": 5, **context["filters"]}:
                    return {"status": "reused", "source": "search.candidates dans le contexte initial"}
                found = search_candidates(task, requested, catalogs=self.catalogs, context=context, accept=accept)
                proposed.extend(found["candidates"])
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
        elif task == "budget":
            validate = self.validate_budget
        else:
            def approve(message):
                if approval:
                    return {"approved": approval["approved"], "already_asked": True}
                approval.update(tools.ask_user(message), content=message)
                return {"approved": approval["approved"]}

            agent.fonctions, agent.schemas = {"ask_user": approve}, [tools.SCHEMA_ASK_USER]

            def validate(text):
                if self.event_data["requirements"].get("document_kind") == "email":
                    if not approval:
                        raise ValueError("Présentez le brouillon complet avec ask_user avant de conclure.")
                    if approval["approved"] and text.strip() != approval["content"].strip():
                        raise ValueError("Le texte final doit être exactement le brouillon approuvé.")
        self.counters["delegations"] += 1
        is_email = task == "documents" and self.event_data["requirements"].get("document_kind") == "email"
        try:
            output = agent.run(encode(payload), counters=self.counters, validate=validate,
                               completed=(lambda: bool(approval)) if is_email else None)
            if task == "documents" and approval and not approval["approved"]:
                outcome = {"status": "refused", "error": "Brouillon d'email refusé ; aucun envoi.",
                           "result": {"content": approval["content"], "approved": False}}
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
        outcome = self.record(task, outcome)
        return outcome

    def record(self, task, outcome):
        outcome = {"task": task, **outcome}
        self.event_data["tasks"][task] = {key: value for key, value in outcome.items() if key != "result"}
        if outcome["status"] == "success":
            field = {"provider": "providers", "documents": "documents"}.get(task, task)
            self.event_data[field] = [outcome["result"]] if field in ("providers", "documents") else outcome["result"]
            if task in CATALOGS:
                outcome["details"] = self.selection_details(task)
        elif task == "documents" and outcome["status"] == "refused":
            self.event_data["documents"] = [outcome["result"]]
        return outcome

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
        # Sans liste annoncée, le Supervisor reste libre de déléguer une autre tâche.
        return self.scope_declared and self.settled()

    def validate_final(self, text):
        if not self.settled():
            raise ValueError("Aucun résultat ou tâches encore incomplètes : poursuivez les délégations utiles.")

    def successful(self):
        return self.settled() and any(value["status"] == "success" for value in self.event_data["tasks"].values()) and all(
            value["status"] in ("success", "skipped") for value in self.event_data["tasks"].values())

    def costs(self):
        req, known = self.event_data["requirements"], {}
        for category in CATALOGS:
            row = self.selected_row(category)
            if not row:
                continue
            if category == "venue":
                period = tools._normalize(self.context("venue")["filters"].get("rental_period") or "journee")
                known["venue_ttc"] = row["prix"][f"location_{period}_ttc"] + row["prix"]["nettoyage_obligatoire_ttc"]
            elif category == "catering" and req.get("guests"):
                known["catering_ttc"] = round(self.selection_details(category)["offer"]["prix_par_personne_ttc"] * req["guests"] + row["frais_supplementaires"]["livraison_forfait_ttc"], 2)
            elif category == "provider":
                known["provider_ht"] = self.selection_details(category)["offer"]["prix_estime_eur_ht"]
        return {"known": known, "budget_total": req.get("budget_total"),
                "allocations": ({key: round(req["budget_total"] * value, 2) for key, value in self.event_data["budget"].items()}
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
        if self.event_data["planning"]:
            planning = self.event_data["planning"]
            lines.append(f"Date proposée : du {planning['start']} au {planning['end']}.")
        for category, label in (("venue", "Salle"), ("catering", "Traiteur"), ("provider", "Prestataire")):
            details = self.selection_details(category)
            if details:
                offer = details.get("offer")
                lines.append(f"{label} : {details['nom']} ({details['id']})" + (f", offre {offer['id']} : {offer.get('description', '')}" if offer else "") + ".")
        costs = self.costs()
        if costs["budget_total"] is not None:
            lines.append(f"Budget plafond : {costs['budget_total']} EUR ; allocations : {encode(costs['allocations'])}.")
        if costs["known"]:
            lines.append("Montants connus (EUR) : " + encode(costs["known"]) + ". " + costs["warning"])
        for task, outcome in self.event_data["tasks"].items():
            if outcome["status"] not in ("success", "skipped"):
                lines.append(f"Non résolu — {task} : {outcome.get('error', 'tâche non terminée')}.")
        for document in self.event_data["documents"]:
            lines.append(("Brouillon refusé :\n" if document.get("approved") is False else "Document :\n") + document["content"])
        assumptions = self.event_data["requirements"].get("assumptions")
        if assumptions:
            lines.append("Hypothèses : " + assumptions)
        venue = self.selected_row("venue")
        if venue:
            lines.append("Contraintes de la salle : " + encode(venue["contraintes"]))
        lines.append("Disponibilités non vérifiées ; aucune réservation confirmée. Aucun email envoyé.")
        return "\n".join(lines)
