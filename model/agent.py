import json

from model import trace
from model.llm import (chat, lire_arguments, message_assistant, tool_message,
                       tool_schema, count_llm_call, run_counters, ErreurLLM, SortieInterrompue)


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

    def run(self, request, *, counters=None, validate=None, completed=None, summary=None, compact_output=False,
            completed_output=None):
        """Traite une demande et renvoie la réponse finale (texte)."""
        token = run_counters.set(counters) if counters is not None else None
        try:
            return self._run(request, validate, completed, summary, compact_output, completed_output)
        finally:
            if token is not None:
                run_counters.reset(token)

    def _run(self, request, validate, completed, summary, compact_output, completed_output):
        messages = [
            {"role": "system", "content": self.instructions},
            {"role": "user", "content": request},
        ]
        trace.tracer(self.name, "DEMANDE", request)

        cache = {}
        observations = []
        corrections = 0
        stagnant = 0
        reason = "Limite d'itérations atteinte."
        allow_final = True
        self.last_status = "running"
        self.last_reason = ""
        self.last_error_code = None
        for _ in range(self.max_iterations):
            try:
                count_llm_call()
                message = chat(messages, tools=None if compact_output and corrections else self.schemas or None, agent=self.name)
            except SortieInterrompue as error:
                reason = str(error)
                self.last_error_code = "interrupted_output"
                if compact_output and corrections < 1:
                    corrections += 1
                    messages.append({"role": "user", "content": reason +
                        ". Retournez uniquement l'objet JSON compact de sélection parmi les candidats fournis. Aucun outil ni explication."})
                    continue
                allow_final = False
                break
            except ErreurLLM as error:
                reason = str(error)
                self.last_error_code = "llm_error"
                allow_final = False
                break

            if compact_output and corrections and message.tool_calls:
                reason = "Réparation invalide : un objet JSON final est attendu, sans appel d'outil."
                self.last_error_code = "invalid_output"
                allow_final = False
                break
            # Pas d'outil demandé : c'est la réponse finale
            if not message.tool_calls:
                texte = message.content or ""
                try:
                    if not texte.strip():
                        raise ValueError("Réponse vide.")
                    if validate:
                        validate(texte)
                    if completed and not summary and not completed():
                        raise ValueError("Tâches encore incomplètes : poursuivez les délégations utiles.")
                except (ValueError, TypeError, KeyError) as error:
                    reason = f"Sortie invalide : {error}"
                    trace.tracer(self.name, "INFO", reason, code="invalid_output")
                    self.last_error_code = "invalid_output"
                    if corrections >= 1:
                        allow_final = False
                        break
                    corrections += 1
                    messages.extend([{"role": "assistant", "content": texte},
                                     {"role": "user", "content": reason + " Corrigez une seule fois." +
                                      (" Retournez uniquement l'objet JSON compact de sélection parmi les candidats fournis. Aucun outil ni explication."
                                       if compact_output else "")}])
                    continue
                self.last_status = "success"
                if summary:
                    texte = summary("Certaines tâches n'ont pas abouti.")
                trace.response(self.name, texte)
                return texte

            # Le modèle a parfois écrit son raisonnement avant d'appeler un outil
            if message.content:
                trace.think(self.name, message.content)

            self.prepare_tool_batch(message.tool_calls)
            messages.append(message_assistant(message))
            progress = False
            for appel in message.tool_calls:
                arguments = lire_arguments(appel)
                key = (appel.function.name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
                function = self.fonctions.get(appel.function.name)
                delegation = getattr(function, "is_delegation", False) is True
                if key in cache and not delegation:
                    # La première observation reste dans l'historique, sans recopier son JSON.
                    resultat = json.dumps({"status": "reused", "tool_call_id": cache[key][0]})
                else:
                    resultat = self.execute_tool(appel)
                    previous = cache.get(key)
                    progress |= previous is None or previous[1] != resultat
                    cache[key] = (appel.id, resultat)
                    observations.append({"tool": appel.function.name, "result": resultat})
                messages.append(tool_message(appel, resultat))
                if completed_output:
                    # Un contenu validé par l'humain termine immédiatement la délégation.
                    texte = completed_output()
                    if texte is not None:
                        self.last_status = "success"
                        self.last_error_code = None
                        trace.response(self.name, texte)
                        return texte
                if completed and completed():
                    # Répondre aux autres tool_call_id sans lancer de travail devenu inutile.
                    for skipped in message.tool_calls[message.tool_calls.index(appel) + 1:]:
                        messages.append(tool_message(skipped, {"status": "skipped", "reason": "Tâches terminées."}))
                    break
            stagnant = 0 if progress else stagnant + 1
            if completed and completed():
                reason = "Tâches terminées."
                break
            if stagnant >= 2:
                reason = "Arrêt : appels répétés sans information nouvelle."
                break

        # Le bilan Python du Supervisor est déjà validé : aucune réécriture LLM.
        # Un spécialiste sans bilan conserve son dernier tour de restitution.
        self.last_status = "partial"
        self.last_reason = reason
        if summary:
            return summary(reason)
        snapshot = json.dumps({"status": "partial", "reason": reason,
                               "observations": observations}, ensure_ascii=False)
        final_messages = messages + [{"role": "user", "content":
            "Restituez les résultats obtenus. Aucun nouvel outil. Si incomplet, dites-le explicitement."}]
        if not allow_final or (compact_output and corrections):
            return snapshot
        try:
            count_llm_call()
            final = chat(final_messages, tools=None, agent=self.name)
            if final.tool_calls or not final.content:
                raise ValueError("Finalisation absente.")
            if validate:
                validate(final.content)
            if completed and not summary and not completed():
                # L'état Python fait foi : pas de faux succès en texte libre.
                return snapshot
            self.last_status = "success"
            return final.content
        except (ErreurLLM, ValueError, TypeError, KeyError) as error:
            self.last_reason = str(error)
            self.last_error_code = ("interrupted_output" if isinstance(error, SortieInterrompue)
                                    else "llm_error" if isinstance(error, ErreurLLM) else "invalid_output")
            return snapshot

    def prepare_tool_batch(self, appels):
        """Point d'extension avant un lot d'outils ; les agents ordinaires n'annoncent aucune tâche."""

    def execute_tool(self, appel):
        """Exécute l'outil demandé par le modèle et renvoie le résultat sous forme de texte."""
        nom_outil = appel.function.name
        arguments = lire_arguments(appel)
        trace.action(self.name, f"{nom_outil} : {arguments}")

        if arguments.get("code") == "invalid_arguments":
            resultat = arguments
        elif nom_outil not in self.fonctions:
            resultat = {"error": f"Outil inconnu : {nom_outil}", "code": "unknown_tool"}
        else:
            try:
                resultat = self.fonctions[nom_outil](**arguments)
            except Exception as erreur:  # on renvoie l'erreur au modèle au lieu de planter
                resultat = {"error": f"Erreur pendant l'exécution de {nom_outil} : {erreur}",
                            "code": "tool_execution_error"}

        if not isinstance(resultat, str):
            resultat = json.dumps(resultat, ensure_ascii=False, separators=(",", ":"))
        trace.observation(self.name, resultat)
        return resultat

    def as_tool(self, nom_outil, description, *, handler=None, properties=None, required=None):
        """
        Transforme cet agent en outil utilisable par un autre agent (pattern Supervisor).
        Renvoie un couple (fonction, schema), comme les autres outils.
        """
        def delegate(demande=None, *, instruction=None, **kwargs):
            if instruction is not None:
                if demande is not None and instruction != demande:
                    raise ValueError("Instructions de délégation contradictoires.")
                demande = instruction
            return handler(self, demande, **kwargs) if handler else self.run(demande)

        delegate.is_delegation = True

        schema = tool_schema(nom_outil, description, properties or {
            "demande": {"type": "string",
                        "description": "La demande complète et précise à transmettre à cet agent."},
        }, requis=required)
        return delegate, schema
