"""Supervisor IA : choisit les tâches et leur ordre, Python conserve leurs résultats."""

from . import trace
from .agent import Agent
from .agents import create_all_agents
from .agents.search import CATALOGS
from .event_state import EventRun, TASKS


PROMPT_SUPERVISOR = """
Tu coordonnes Planning, Budgetary, Decision-Making et Writer pour une demande indépendante.
Délègue directement une instruction aux spécialistes utiles. Aucune configuration préalable.
La demande originale leur est transmise automatiquement.
Tu peux joindre requirements avec seulement les contraintes explicites déjà connues
(city, guests, budget_total, date, weekday, planning, rental_period, preferences,
music_end, music_required, document_kind ou venue_filters/catering_filters/provider_filters).
Ajoute les nouvelles contraintes au moment utile, sans recopier celles déjà enregistrées.
Pour une demande multiple, indique si possible tasks dès la première délégation :
cette liste facultative sert à reconnaître la fin, pas à imposer l'ordre des tâches.
N'invente ni effectif ni plafond. budget_total=null signifie sans plafond connu/illimité.
Une soirée générique n'est pas soiree_etudiante : ne crée pas ce filtre.
Transmets seulement les informations explicites, les préférences et les tâches utiles.
Une ville d'événement ne signifie pas que les prestataires doivent y avoir leur siège.
Ne prépare pas les filtres de recherche : Python les prépare lors des délégations.
Par exemple planning_agent(demande="Propose une date", tasks=["planning","venue","catering","provider"]).
Pour une soirée, rental_period=soiree est facultatif. Ne fabrique aucune valeur inconnue.
Sans date imposée, omets date et planning ; laisse Planning choisir une date future.
Pour un samedi sans date exacte, weekday=saturday ; ne calcule pas toi-même la date.
Budgetary répartit seulement un budget connu ; ne le délègue pas sans plafond connu.
Si un plafond est explicite, joins budget_total avant les sélections ; Budgetary le répartit.
Après un échec, continue les tâches indépendantes. Ne redélègue pas une tâche terminée.
Si la date ET les horaires sont imposés, fournis planning et évite planning_agent.
Si seule la date est imposée, fournis date et laisse Planning proposer les horaires.
Python conserve les exigences et les résultats : ne les recopie pas.
Choisis l'ordre selon les besoins : généralement planning, budget si limité, salle,
puis traiteur et prestataire. Une délégation Decision-Making concerne exactement
venue, catering OU provider. Python injecte les contraintes et les candidats compatibles.
La date n'est pas indispensable pour recommander une salle : disponibilité à confirmer.
Si tu recherches sans requirements, utilise l'outil de recherche du spécialiste pour
appliquer les critères de la demande originale avant de choisir.
Ne refais pas un choix qualitatif validé. Ne redélègue pas un succès ou un conflit
sans information nouvelle. Explique les conflits obligatoires, sans les assouplir.
Les montants HT et TTC ne doivent pas être additionnés comme un coût final garanti.
Writer est réservé aux documents explicitement demandés ; une réponse simple n'en a pas besoin.
Les emails sont des brouillons soumis à ask_user ; aucun envoi automatique.
Quand les tâches sont terminées, donne une seule réponse finale concise en français,
avec noms, dates, offres et réserves utiles. Les catalogues ne prouvent aucune disponibilité.
Si une tâche échoue ou une limite est atteinte, présente les résultats partiels et les conflits.
"""


class Supervisor(Agent):
    def __init__(self, max_iterations=6, max_llm_calls=24, max_delegations=8):
        super().__init__("Supervisor", PROMPT_SUPERVISOR, max_iterations=max_iterations)
        if any(type(value) is not int or value < 1 for value in (max_iterations, max_llm_calls, max_delegations)):
            raise ValueError("Les limites doivent être des entiers positifs.")
        self.max_llm_calls, self.max_delegations = max_llm_calls, max_delegations
        self._bind(None)

    def _bind(self, run):
        budget, writer, planning, decision = create_all_agents()
        definitions = []
        for agent, name, task in ((budget, "budgetary_agent", "budget"), (writer, "writer_agent", "documents"),
                                  (planning, "planning_agent", "planning"), (decision, "decision_making_agent", None)):
            properties = {
                "demande": {"type": "string"},
                "requirements": {"type": "object", "description": "Facultatif : contraintes explicites nouvelles seulement, sans champs vides."},
                "tasks": {"type": "array", "items": {"type": "string", "enum": list(TASKS)},
                          "description": "Facultatif : tâches demandées, sans ordre imposé."},
            }
            if task is None:
                properties["category"] = {"type": "string", "enum": list(CATALOGS)}

            def handler(agent, demande, category=None, requirements=None, tasks=None, task=task):
                return run.delegate(agent, demande, task or category, requirements=requirements, tasks=tasks)

            definitions.append(agent.as_tool(name, f"Délègue {task or 'une sélection par catégorie'} ; contexte ajouté par Python.",
                                             handler=handler, properties=properties,
                                             required=["demande"] + (["category"] if task is None else [])))
        self.fonctions = {schema["function"]["name"]: function for function, schema in definitions}
        self.schemas = [schema for _, schema in definitions]

    def run(self, request):
        run = EventRun(request, self.max_llm_calls, self.max_delegations)
        self._bind(run)
        # Diagnostic du dernier run seulement ; remplacé intégralement à chaque demande.
        self.event_data, self.counters = run.event_data, run.counters
        try:
            response = super().run(request, counters=run.counters, completed=run.complete,
                                   validate=run.validate_final, summary=run.summary)
            self.last_status = "success" if run.successful() else "partial"
            if not run.successful():
                response = run.summary(self.last_reason or "Certaines tâches n'ont pas abouti.")
            trace.tracer(self.name, "INFO", f"État : {self.last_status}", **run.counters)
            return response
        except Exception as error:
            self.last_status = "partial"
            return run.summary(f"Exécution interrompue : {error}")
        finally:
            # Libérer les catalogues et le registre capturés dans les délégations.
            self._bind(None)


def create_orchestrator(*, max_iterations=6, max_llm_calls=24, max_delegations=8) -> Agent:
    return Supervisor(max_iterations, max_llm_calls, max_delegations)
