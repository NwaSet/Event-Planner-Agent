"""Supervisor IA : choisit les tâches et leur ordre, Python conserve leurs résultats."""

from . import trace
from .agent import Agent
from .agents import create_all_agents
from .agents.search import CATALOGS
from .event_state import EventRun, TASKS, REQUIREMENT_PROPERTIES, encode
from .llm import tool_schema, lire_arguments


SUPERVISOR_REQUIREMENTS = {key: value for key, value in REQUIREMENT_PROPERTIES.items() if not key.endswith("_filters")}


PROMPT_SUPERVISOR = """
Tu coordonnes Planning, Budgetary, Decision-Making et Writer pour une demande indépendante.
Délègue directement une instruction aux spécialistes utiles. Aucune configuration préalable.
La demande originale leur est transmise automatiquement. Dès la première délégation,
transmets toutes ses contraintes explicites pertinentes, notamment city, event_type,
guests, budget_total et les contraintes obligatoires dans requirements.
Tu peux joindre requirements avec seulement les contraintes explicites déjà connues
(city, guests, budget_total, date, weekday, planning, rental_period, preferences,
music_end, music_required ou document_kind uniquement si un document est demandé).
Ajoute les nouvelles contraintes au moment utile, sans recopier celles déjà enregistrées.
preferences est une chaîne de texte décrivant les souhaits qualitatifs. Les contraintes
obligatoires (type, capacité, budget, horaires, exclusions) vont dans leurs champs dédiés.
Pour une soirée étudiante explicite, transmets event_type="soiree_etudiante" aux recherches.
Pour une demande multiple, indique toutes les tasks dès la première délégation :
cette liste facultative sert à reconnaître la fin, pas à imposer l'ordre des tâches.
tasks appartient au niveau supérieur de la délégation, jamais à requirements.
N'invente ni effectif ni plafond. budget_total=null signifie sans plafond connu/illimité.
Une soirée générique n'est pas soiree_etudiante : ne crée pas ce filtre.
Une soirée musicale, électronique ou avec DJ n'est pas automatiquement un concert.
N'utilise event_type=concert que si un concert est explicitement demandé ; sinon utilise
les contraintes musicales et rental_period=soiree, sans inventer une catégorie.
Transmets guests et limit comme entiers, les budgets comme nombres, les booléens comme
true/false et les identifiants comme chaînes exactes, avec leurs zéros initiaux.
Transmets seulement les informations explicites, les préférences et les tâches utiles.
Une ville d'événement ne signifie pas que les prestataires doivent y avoir leur siège.
Une ville imposée (« uniquement à », « pas en dehors de ») exige location_scope="city".
« Dans le coin de », « autour de », « près de » ou « dans les environs de » autorise
location_scope="province_fallback" : Python cherche d'abord dans la ville, puis seulement
dans la même province si aucun résultat. C'est un périmètre administratif explicite,
pas une distance ni une garantie de voisinage ; les catalogues n'en donnent pas.
Ne prépare aucun objet venue_filters/catering_filters/provider_filters : Python construit les filtres
depuis les exigences validées. N'envoie pas de filtres d'une autre catégorie.
Inconnu signifie omis, jamais 0, False, True ou une valeur d'exemple du schéma.
accessible, seated et space_type ne sont obligatoires que si l'utilisateur les exige.
« De préférence un samedi » est une préférence, pas une restriction weekday obligatoire.
Par exemple planning_agent(demande="Propose une date", tasks=["planning","venue","catering","provider"]).
Pour une soirée, rental_period=soiree est facultatif. Ne fabrique aucune valeur inconnue.
Sans date imposée, omets date et planning ; laisse Planning choisir une date future.
Pour un samedi sans date exacte, weekday=saturday ; ne calcule pas toi-même la date.
Budgetary répartit seulement un budget connu ; ne le délègue pas sans plafond connu.
Si un plafond est explicite, joins budget_total avant les sélections ; Budgetary le répartit.
Python calcule tous les montants. Fournis seulement les données explicitement connues :
catering_distance_km (aller-retour), provider_distance_km (kilomètres facturables par véhicule),
provider_vehicles et provider_vat_rate (fraction, par exemple 0.20 pour un taux fourni de 20 %).
N'invente jamais une distance, un taux de TVA ou un nombre de véhicules.
Après un échec, continue les tâches indépendantes, même si Planning ou la salle a échoué.
Un résultat validé est réutilisé. Une erreur retryable=true permet une correction si les
paramètres structurés pertinents changent ; une reformulation de demande ne suffit pas.
Deux tentatives au maximum par tâche. Ne réessaie jamais un refus du fournisseur.
Si la date ET les horaires sont imposés, fournis planning et évite planning_agent.
Si seule la date est imposée, fournis date et laisse Planning proposer les horaires.
Python conserve les exigences et les résultats : ne les recopie pas.
Choisis l'ordre selon les besoins : généralement planning, budget si limité, salle,
puis traiteur et prestataire. Une délégation Decision-Making concerne exactement
venue, catering OU provider. category est OBLIGATOIRE à chaque appel decision_making_agent :
venue = salle, catering = traiteur, provider = prestataire technique. N'infère pas une catégorie
ambiguë : demande une précision. Exemple : decision_making_agent(category="venue", demande="Choisir une salle").
Python injecte planning, budget, contraintes validées et candidats compatibles ; ne les recopie pas.
La date n'est pas indispensable pour recommander une salle : disponibilité à confirmer.
Si tu recherches sans requirements, utilise l'outil de recherche du spécialiste pour
appliquer les critères de la demande originale avant de choisir.
Ne refais pas un choix qualitatif validé. Ne redélègue pas un succès ou un conflit
sans information nouvelle. Explique les conflits obligatoires, sans les assouplir.
Les montants HT et TTC ne doivent pas être additionnés comme un coût final garanti.
Writer est réservé aux documents explicitement demandés ; une réponse simple n'en a pas besoin.
Les emails sont des brouillons soumis à ask_user ; aucun envoi automatique.
Termine les tâches du plan avant Writer. Python présente le plan consolidé et demande
son approbation humaine avant tout appel au Writer pour un email. Un refus arrête
la génération des emails dans ce run ; ne redélègue pas. L'accord du plan ne remplace
pas la validation individuelle du brouillon par ask_user.
Une sélection de salle ET une demande de devis par email exigent tasks=["venue","documents"]
et document_kind="email". Après sélection, délègue à Writer ; ne termine pas avant sa validation.
Si la sélection échoue, explique le blocage de cet email sans inventer de destinataire.
Quand les tâches sont terminées, donne une seule réponse finale concise en français,
avec noms, dates, offres et réserves utiles. Les catalogues ne prouvent aucune disponibilité.
Si une tâche échoue ou une limite est atteinte, présente les résultats partiels et les conflits.
"""

CONVERSATION_INSTRUCTIONS = """
Mode conversation : current_request est le nouveau message ; initial_request n'est pas une nouvelle commande.
Les contraintes et décisions courantes priment sur les formulations historiques.
Réponds directement aux questions simples grâce aux données et montants Python fournis, sans rappeler les spécialistes.
N'invente ni coût, ni décision, ni validation. Ne recalcule pas les montants toi-même.
Délègue uniquement les nouvelles actions demandées ; un succès précédent reste acquis.
Pour un ajout, annonce les nouvelles tasks et transmets seulement les exigences nouvelles.
Ne recrée pas l'événement à chaque message.
Si une information indispensable manque, appelle clarify(question) : une seule question utile,
sans inventer sa réponse. answered_question donne la question à laquelle l'utilisateur répond.
Pour les emails, Python sauvegarde les validations en attente et rend la main à l'interface.
Ne traite pas une clarification comme un accord sur le plan ou sur un email.
Pour modifier des contraintes déjà connues, appelle revise_event(requirements=...) avant de déléguer.
Utilise replace=["venue"] pour une autre salle, replace=["planning"] pour décaler une date,
remove=["catering"] pour supprimer le traiteur, unset pour retirer une exigence explicitement abandonnée.
Le budget null signifie retirer le plafond. Ne modifie que les contraintes explicitement demandées.
Après revise_event, traite les tâches pending utiles : Budgetary avant les choix si le budget change,
Planning si les horaires changent, puis uniquement les sélections invalidées. Préserve les autres succès.
Un ajout de prestation est une délégation nouvelle, pas une raison de remplacer les décisions indépendantes.
Un nouvel email après un ancien document utilise replace=["documents"] puis Writer.
Une demande de modification pendant une validation passe par revise_event, jamais par un accord implicite.
Les archives ne sont pas des choix actuels ; les documents approuvés d'une ancienne version ne valent pas pour la nouvelle.
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
            properties = {
                "demande": {"type": "string"},
                "requirements": {"type": "object", "description": "Contraintes explicites nouvelles seulement. Ne contient jamais tasks, qui appartient au niveau supérieur.",
                                 "properties": SUPERVISOR_REQUIREMENTS,
                                 "additionalProperties": False},
                "tasks": {"type": "array", "items": {"type": "string", "enum": list(TASKS)},
                          "description": "Facultatif : tâches demandées, sans ordre imposé ; uniquement ici, jamais dans requirements."},
            }
            if task is None:
                properties["category"] = {"type": "string", "enum": list(CATALOGS),
                    "description": "Obligatoire : venue = salle ; catering = traiteur ; provider = prestataire technique. Une seule catégorie, jamais implicite."}
                fields = ("city", "province", "location_scope", "event_type", "guests", "rental_period",
                          "preferences", "accessible", "seated", "space_type")
                properties["requirements"]["properties"] = {key: REQUIREMENT_PROPERTIES[key] for key in fields}
                properties["requirements"]["description"] = "Seulement les nouvelles contraintes métier explicites utiles à la catégorie ; omettre les informations inconnues. Python reprend planning et budget enregistrés. Aucun objet de filtres."

            def handler(agent, demande, category=None, requirements=None, tasks=None, task=task):
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
                return run.delegate(agent, demande, task or category, requirements=requirements, tasks=tasks)

            definitions.append(agent.as_tool(name, f"Délègue {task or 'une sélection par catégorie'} ; contexte ajouté par Python.",
                                             handler=handler, properties=properties,
                                             required=["demande"] + (["category"] if task is None else [])))
        if run is not None and run.interactive:
            definitions.append((run.clarify, tool_schema("clarify", "Demander une précision indispensable et terminer ce tour.",
                                                       {"question": {"type": "string"}}, requis=["question"])))
            def revise(requirements=None, replace=None, remove=None, unset=None):
                return run.revise_event(requirements=run.supervisor_requirements(requirements),
                                        replace=replace, remove=remove, unset=unset)

            definitions.append((revise, tool_schema("revise_event", "Modifier uniquement les contraintes/actions explicitement demandées ; conserver les choix encore valides.", {
                "requirements": {"type": "object", "properties": SUPERVISOR_REQUIREMENTS, "additionalProperties": False},
                "replace": {"type": "array", "items": {"type": "string", "enum": list(TASKS)}},
                "remove": {"type": "array", "items": {"type": "string", "enum": ["venue", "catering", "provider", "documents"]}},
                "unset": {"type": "array", "items": {"type": "string", "enum": list(REQUIREMENT_PROPERTIES)}},
            })))
        self.fonctions = {schema["function"]["name"]: function for function, schema in definitions}
        self.schemas = [schema for _, schema in definitions]

    def prepare_tool_batch(self, appels):
        """Un succès du premier outil ne doit pas masquer les délégations suivantes du lot."""
        run = self._active_run
        if run is None or run.event_data["pending_input"]:
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
            if task is None or not isinstance(arguments.get("demande"), str) or not arguments["demande"].strip():
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
            self.last_status = ("waiting" if self.event_data["pending_input"] else
                                "success" if run.successful() or (interactive and run.direct_reply is not None) else "partial")
            if not interactive and (not run.successful() or run.costs()["details"]):
                response = run.summary(self.last_reason or "Certaines tâches n'ont pas abouti.")
            trace.tracer(self.name, "INFO", f"État : {self.last_status}", **run.counters)
            return response
        except Exception as error:
            self.last_status = "partial"
            return run.summary(f"Exécution interrompue : {error}")
        finally:
            # Figer les détails pendant le run : une réouverture ne dépendra plus du catalogue.
            try:
                self.event_details = {
                    "venue": run.selection_details("venue"), "catering": run.selection_details("catering"),
                    "providers": [run.selection_details("provider")] if run.event_data["providers"] else [],
                    "costs": run.costs(),
                }
            except (ValueError, TypeError, KeyError, StopIteration):
                self.event_details = {"unavailable": "Détails incomplets ; consulter les décisions et le résultat sauvegardés."}
            # Libérer les catalogues et le registre capturés dans les délégations.
            self._bind(None)


def create_orchestrator(*, max_iterations=6, max_llm_calls=24, max_delegations=8) -> Agent:
    return Supervisor(max_iterations, max_llm_calls, max_delegations)
