import json

from model import trace
from model.llm import chat, lire_arguments, message_assistant, message_outil, schema_outil


class Agent:
    def __init__(self, nom, instructions, outils=None, max_iterations=6):
        self.nom = nom
        self.instructions = instructions
        self.max_iterations = max_iterations
        outils = outils or []
        # nom de l'outil -> fonction Python à exécuter
        self.fonctions = {schema["function"]["name"]: fonction for fonction, schema in outils}
        # descriptions envoyées au modèle
        self.schemas = [schema for _, schema in outils]

    def run(self, demande):
        """Traite une demande et renvoie la réponse finale (texte)."""
        messages = [
            {"role": "system", "content": self.instructions},
            {"role": "user", "content": demande},
        ]
        trace.tracer(self.nom, "DEMANDE", demande)

        for _ in range(self.max_iterations):
            message = chat(messages, tools=self.schemas or None, agent=self.nom)

            # Pas d'outil demandé : c'est la réponse finale
            if not message.tool_calls:
                texte = message.content or ""
                trace.response(self.nom, texte)
                return texte

            # Le modèle a parfois écrit son raisonnement avant d'appeler un outil
            if message.content:
                trace.think(self.nom, message.content)

            messages.append(message_assistant(message))
            for appel in message.tool_calls:
                resultat = self.executer_outil(appel)
                messages.append(message_outil(appel, resultat))

        trace.tracer(self.nom, "INFO", f"Limite de {self.max_iterations} itérations atteinte : arrêt.")
        return "Je n'ai pas pu terminer : limite d'itérations atteinte."

    def executer_outil(self, appel):
        """Exécute l'outil demandé par le modèle et renvoie le résultat sous forme de texte."""
        nom_outil = appel.function.name
        arguments = lire_arguments(appel)
        trace.action(self.nom, nom_outil, arguments)

        if nom_outil not in self.fonctions:
            resultat = f"Erreur : l'outil '{nom_outil}' n'existe pas. Outils disponibles : {list(self.fonctions)}"
        else:
            try:
                resultat = self.fonctions[nom_outil](**arguments)
            except Exception as erreur:  # on renvoie l'erreur au modèle au lieu de planter
                resultat = f"Erreur pendant l'exécution de {nom_outil} : {erreur}"

        if not isinstance(resultat, str):
            resultat = json.dumps(resultat, ensure_ascii=False)
        trace.observation(self.nom, resultat)
        return resultat

    def comme_outil(self, nom_outil, description):
        """
        Transforme cet agent en outil utilisable par un autre agent (pattern Supervisor).
        Renvoie un couple (fonction, schema), comme les autres outils.
        """
        def deleguer(demande):
            return self.run(demande)

        schema = schema_outil(nom_outil, description, {
            "demande": {"type": "string",
                        "description": "La demande complète et précise à transmettre à cet agent."},
        })
        return deleguer, schema
