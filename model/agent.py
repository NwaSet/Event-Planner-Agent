import json

from model import trace
from model.llm import chat, lire_arguments, message_assistant, tool_message, tool_schema


class Agent:
    def __init__(self, name, instructions, tools=None, max_iterations=6):
        self.name = name
        self.instructions = instructions
        self.max_iterations = max_iterations
        tools = tools or []
        # nom de l'outil -> fonction Python à exécuter
        self.fonctions = {schema["function"]["name"]: fonction for fonction, schema in tools}
        # descriptions envoyées au modèle
        self.schemas = [schema for _, schema in tools]

    def run(self, request):
        """Traite une demande et renvoie la réponse finale (texte)."""
        messages = [
            {"role": "system", "content": self.instructions},
            {"role": "user", "content": request},
        ]
        trace.tracer(self.name, "DEMANDE", request)

        for _ in range(self.max_iterations):
            message = chat(messages, tools=self.schemas or None, agent=self.name)

            # Pas d'outil demandé : c'est la réponse finale
            if not message.tool_calls:
                texte = message.content or ""
                trace.response(self.name, texte)
                return texte

            # Le modèle a parfois écrit son raisonnement avant d'appeler un outil
            if message.content:
                trace.think(self.name, message.content)

            messages.append(message_assistant(message))
            for appel in message.tool_calls:
                resultat = self.execute_tool(appel)
                messages.append(tool_message(appel, resultat))

        trace.tracer(self.name, "INFO", f"Limite de {self.max_iterations} itérations atteinte : arrêt.")
        return "Je n'ai pas pu terminer : limite d'itérations atteinte."

    def execute_tool(self, appel):
        """Exécute l'outil demandé par le modèle et renvoie le résultat sous forme de texte."""
        nom_outil = appel.function.name
        arguments = lire_arguments(appel)
        trace.action(self.name, nom_outil, arguments)

        if nom_outil not in self.fonctions:
            resultat = f"Erreur : l'outil '{nom_outil}' n'existe pas. Outils disponibles : {list(self.fonctions)}"
        else:
            try:
                resultat = self.fonctions[nom_outil](**arguments)
            except Exception as erreur:  # on renvoie l'erreur au modèle au lieu de planter
                resultat = f"Erreur pendant l'exécution de {nom_outil} : {erreur}"

        if not isinstance(resultat, str):
            resultat = json.dumps(resultat, ensure_ascii=False)
        trace.observation(self.name, resultat)
        return resultat

    def as_tool(self, nom_outil, description):
        """
        Transforme cet agent en outil utilisable par un autre agent (pattern Supervisor).
        Renvoie un couple (fonction, schema), comme les autres outils.
        """
        def delegate(demande):
            return self.run(demande)

        schema = tool_schema(nom_outil, description, {
            "demande": {"type": "string",
                        "description": "La demande complète et précise à transmettre à cet agent."},
        })
        return delegate, schema
