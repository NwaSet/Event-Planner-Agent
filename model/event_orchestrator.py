"""Supervisor IA : choisit les tâches et leur ordre, Python conserve leurs résultats."""

from . import trace
from .agent import Agent
from .agents import create_all_agents
from .agents.search import CATALOGS
from .event_state import EventRun, TASKS, REQUIREMENT_PROPERTIES, encode
from .llm import tool_schema, lire_arguments


SUPERVISOR_REQUIREMENTS = {key: value for key, value in REQUIREMENT_PROPERTIES.items() if not key.endswith("_filters")}


PROMPT_SUPERVISOR = """
Tu coordonnes Planning, Budgetary, Decision-Making et Writer. Tu choisis quand et pourquoi déléguer.
Enregistre les seuls faits explicitement connus avec revise_event(requirements=..., tasks=[...]),
puis délègue une instruction courte : Python injecte le contexte ciblé, ne recopie pas les contraintes.
Annonce les tâches demandées pour reconnaître la fin. Choisis la salle avant les traiteurs et prestataires demandés.
Planning choisit dates et horaires futurs si non imposés ; une date seule ne fixe pas les horaires.
Budgetary répartit un budget global connu avant les achats, puis réévalue l'allocation si une prestation est ajoutée.
Decision-Making exige category : venue (salle), catering (traiteur), provider (technique).
Exemple : decision_making_agent(category="venue", instruction="Choisir une salle adaptée").
Ne refais pas un succès compatible ni une recherche no_match identique. Après un échec, poursuis les tâches indépendantes.
Les erreurs réparables ont au plus deux tentatives avec des paramètres utiles différents, pas une simple reformulation.
Pour modifier, utilise revise_event ; replace=["venue"] demande une autre salle, replace=["planning"] une autre date,
remove=["catering"] retire la prestation, unset retire une contrainte abandonnée. Traite les tâches invalidated/pending utiles.
N'invente aucun effectif, plafond, taux TVA, distance, véhicule, accessibilité ou type d'espace.
budget_total=null retire le plafond global ; un plafond de prestataire appartient à provider_constraints.max_budget.
Une soirée générique/musicale/DJ n'est ni une soirée étudiante ni un concert sans demande explicite.
Une préférence de samedi reste preferences, sans weekday obligatoire.
Par défaut, cherche la salle dans la commune compatible la plus proche (location_scope=nearest).
location_scope=city uniquement si la commune est strictement imposée ; province_fallback si la province est imposée.
Après sélection, les recherches de traiteurs, prestataires et météo utilisent la commune réelle de la salle.
La proximité est à vol d'oiseau ; ne garantis ni temps de trajet ni disponibilité.
Les *_constraints contiennent uniquement des exigences métier explicites ; aucun filtre technique ou valeur inventée.
Les montants sont calculés par Python ; ne mélange pas HT, TTC et caution. N'invente aucune réservation.
Writer seulement pour un document explicitement demandé, après les autres tâches utiles.
Pour un email, document_kind=email et tasks inclut documents : Python soumet d'abord le plan global à validation.
Un refus interdit la génération ; un accord du plan ne remplace pas la validation individuelle du brouillon.
Aucun envoi automatique. Termine par une réponse française concise ; les résultats partiels et conflits restent visibles.
"""

CONVERSATION_INSTRUCTIONS = """
current_request est le nouveau message. L'état courant prime sur l'historique conservé séparément.
Réponds directement aux questions simples avec les décisions et montants fournis, sans redélégation.
clarify(question) demande une précision indispensable ; answered_question identifie la question en cours.
Python sauvegarde les validations en attente ; ne transforme jamais une clarification en accord.
Une modification matérielle invalide les anciens accords. Pour un nouveau document utilise replace=["documents"].
Ne répète pas l'organisation entière à chaque tour : ajoute ou modifie uniquement les contraintes concernées.
"""


class Supervisor(Agent):
    def __init__(self, max_iterations=15, max_llm_calls=24, max_delegations=8):
        super().__init__("Supervisor", PROMPT_SUPERVISOR, max_iterations=max_iterations)
        if any(type(value) is not int or value < 1 for value in (max_iterations, max_llm_calls, max_delegations)):
            raise ValueError("Les limites doivent être des entiers positifs.")
        self.max_llm_calls, self.max_delegations = max_llm_calls, max_delegations
        self._bind(None)

    def _bind(self, run):
        self._active_run = run
        budget, writer, planning, decision = create_all_agents()
        definitions = []
        for agent, name, task in ((budget, "budgetary_agent", "budget"), (writer, "writer_agent", "documents"),
                                  (planning, "planning_agent", "planning"), (decision, "decision_making_agent", None)):
            properties = {"instruction": {"type": "string", "description": "Action ciblée, sans recopier l'état."}}
            if task is None:
                properties["category"] = {"type": "string", "enum": list(CATALOGS),
                    "description": "Obligatoire : venue = salle ; catering = traiteur ; provider = prestataire technique. Une seule catégorie, jamais implicite."}

            def handler(agent, instruction=None, category=None, demande=None, requirements=None, tasks=None, task=task):
                # Lecture des anciens appels simulés ; seul instruction est exposé au modèle.
                instruction = instruction if instruction is not None else demande
                if not isinstance(instruction, str) or not instruction.strip():
                    return {"status": "error", "code": "invalid_arguments", "error": "instruction non vide obligatoire."}
                if task is None and (not isinstance(category, str) or category not in CATALOGS):
                    return {"status": "error", "code": "invalid_arguments",
                            "error": "category est obligatoire : venue (salle), catering (traiteur), provider (prestataire). Aucune catégorie déduite."}
                if run.interactive:
                    if requirements is not None and not isinstance(requirements, dict):
                        return {"status": "error", "code": "invalid_arguments", "error": "requirements doit être un objet JSON, pas une chaîne."}
                    if task == "documents" and not run.document_requested():
                        return {"status": "not_requested", "reason": "Aucun document demandé ; Writer non exécuté."}
                    requirements = run.supervisor_requirements(requirements, category=category if task is None else None)
                    if isinstance(tasks, list) and not run.document_requested():
                        tasks = [value for value in tasks if value != "documents"] or None
                return run.delegate(agent, instruction, task or category, requirements=requirements, tasks=tasks)

            definitions.append(agent.as_tool(name, f"Délègue {task or 'une sélection par catégorie'} ; contexte ajouté par Python.",
                                             handler=handler, properties=properties,
                                             required=["instruction"] + (["category"] if task is None else [])))
        if run is not None:
            if run.interactive:
                definitions.append((run.clarify, tool_schema("clarify", "Demander une précision indispensable et terminer ce tour.",
                                                           {"question": {"type": "string"}}, requis=["question"])))
            def revise(requirements=None, replace=None, remove=None, unset=None, tasks=None):
                if tasks is not None and (not isinstance(tasks, list) or not tasks or any(task not in TASKS for task in tasks)):
                    raise ValueError("tasks : liste non vide de tâches connues attendue.")
                outcome = run.revise_event(requirements=run.supervisor_requirements(requirements),
                                           replace=replace, remove=remove, unset=unset)
                run.remember(tasks=tasks)
                return {**outcome, "tasks": run.event_data["tasks"]}

            definitions.append((revise, tool_schema("revise_event", "Modifier uniquement les contraintes/actions explicitement demandées ; conserver les choix encore valides.", {
                "requirements": {"type": "object", "properties": SUPERVISOR_REQUIREMENTS, "additionalProperties": False},
                "tasks": {"type": "array", "items": {"type": "string", "enum": list(TASKS)}},
                "replace": {"type": "array", "items": {"type": "string", "enum": list(TASKS)}},
                "remove": {"type": "array", "items": {"type": "string", "enum": ["venue", "catering", "provider", "documents"]}},
                "unset": {"type": "array", "items": {"type": "string", "enum": list(REQUIREMENT_PROPERTIES)}},
            }, requis=[])))
        self.fonctions = {schema["function"]["name"]: function for function, schema in definitions}
        self.schemas = [schema for _, schema in definitions]

    def prepare_tool_batch(self, appels):
        """Un succès du premier outil ne doit pas masquer les délégations suivantes du lot."""
        run = self._active_run
        if run is None or run.event_data["workflow"]["pending_input"]:
            return
        planned = []
        declared = False
        for appel in appels:
            arguments = lire_arguments(appel)
            if arguments.get("code") == "invalid_arguments":
                continue
            name = appel.function.name
            if name == "clarify" and isinstance(arguments.get("question"), str) and arguments["question"].strip():
                break  # Aucun travail annoncé après une question qui rend la main à l'utilisateur.
            task = {"planning_agent": "planning", "budgetary_agent": "budget", "writer_agent": "documents"}.get(name)
            if name == "decision_making_agent":
                category = arguments.get("category")
                if not isinstance(category, str) or category not in CATALOGS:
                    continue
                task = category
            instruction = arguments.get("instruction", arguments.get("demande"))
            if task is None or not isinstance(instruction, str) or not instruction.strip():
                continue
            if run.interactive and task == "documents" and not run.document_requested():
                continue
            requirements = arguments.get("requirements")
            if requirements is not None and not isinstance(requirements, dict):
                continue
            tasks = arguments.get("tasks")
            nested = (requirements or {}).get("tasks")
            if nested is not None:
                if not isinstance(nested, list) or not nested or any(value not in TASKS for value in nested):
                    continue
                if tasks is not None and (not isinstance(tasks, list) or any(value not in TASKS for value in tasks)
                                          or set(tasks) != set(nested)):
                    continue
                tasks = nested
            if tasks is not None and (not isinstance(tasks, list) or not tasks or any(value not in TASKS for value in tasks)):
                continue
            announced = tasks or []
            if run.interactive and not run.document_requested():
                announced = [value for value in announced if value != "documents"]
            declared |= bool(announced)
            planned.extend([task, *announced])
        # Sans liste explicite, le Supervisor reste libre de poursuivre au tour ReAct suivant.
        run.scope_declared |= declared
        for task in planned:
            run.event_data["tasks"].setdefault(task, {"status": "pending"})

    def run(self, request, *, event_data=None, messages=None, interactive=False):
        self.event_details = {}
        run = EventRun(request, self.max_llm_calls, self.max_delegations, event_data=event_data, interactive=interactive)
        self._bind(run)
        self.instructions = PROMPT_SUPERVISOR + (CONVERSATION_INSTRUCTIONS if interactive else "")
        # Diagnostic du dernier run seulement ; remplacé intégralement à chaque demande.
        self.event_data, self.counters = run.event_data, run.counters
        try:
            handled = run.resolve_pending(request) if interactive else False
            if run.resume_writer:
                self.fonctions["writer_agent"](**run.resume_writer)
            if handled:
                response = run.turn_summary("Validation enregistrée.")
            else:
                response = super().run(encode(run.conversation_context(messages)) if interactive else request, counters=run.counters,
                                       completed=lambda: run.complete() and not run.can_repair(),
                                       validate=run.validate_final, summary=run.turn_summary if interactive else run.summary)
            self.last_status = ("waiting" if self.event_data["workflow"]["pending_input"] else
                                "success" if run.successful() or (interactive and run.direct_reply is not None) else "partial")
            if not interactive and (not run.successful() or run.costs()["details"]):
                response = run.summary(self.last_reason or "Certaines tâches n'ont pas abouti.")
            trace.tracer(self.name, "INFO", f"État : {self.last_status}", **run.counters)
            return response
        except Exception as error:
            self.last_status = "partial"
            return run.summary(f"Exécution interrompue : {error}")
        finally:
            # Le bilan affiché reste dans messages ; aucune fiche catalogue dupliquée.
            self.event_details = {}
            # Libérer les catalogues et le registre capturés dans les délégations.
            self._bind(None)


def create_orchestrator(*, max_iterations=6, max_llm_calls=24, max_delegations=8) -> Agent:
    return Supervisor(max_iterations, max_llm_calls, max_delegations)
