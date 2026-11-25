"""Exécuter une nouvelle conversation et conserver son résultat ; aucune interface."""

from copy import deepcopy

from dao.conversation_dao import ConversationError, create_conversation, save_conversation


def _create_orchestrator(*, require_configuration=True):
    # Les imports LLM et la configuration ne sont nécessaires qu'à une nouvelle exécution.
    from model import llm
    from model.event_orchestrator import create_orchestrator

    problem = llm.verifier_configuration()
    if not llm.BASE_URL.strip() or not llm.MODELE.strip():
        problem = "Renseignez LLM_BASE_URL et LLM_MODEL dans le fichier .env."
    if problem and require_configuration:
        raise ConversationError(problem)
    llm.nouvelle_demande()
    return create_orchestrator()


def run_conversation(request, *, directory=None, orchestrator_factory=None):
    """Créer une conversation puis traiter son premier message."""
    conversation = create_conversation(request)
    return _run_turn(conversation, request, directory, orchestrator_factory)


def continue_conversation(conversation, request, *, directory=None, orchestrator_factory=None):
    """Un nouveau tour dans le même fichier ; charger seul ne déclenche aucune exécution."""
    if not isinstance(request, str) or not request.strip():
        raise ConversationError("Le message doit être non vide.")
    conversation = deepcopy(conversation)
    conversation["messages"].append({"role": "user", "content": request})
    return _run_turn(conversation, request, directory, orchestrator_factory)


def _run_turn(conversation, request, directory, orchestrator_factory):
    conversation["status"] = "running"
    save_conversation(conversation, directory)  # conserver la demande avant tout appel LLM
    orchestrator = None
    try:
        pending = conversation["event_data"].get("pending_input") or {}
        answer = request.strip().casefold().rstrip(".! ")
        python_only = (pending.get("kind") == "email_approval" and answer in ("oui", "o", "yes", "y", "non", "n", "no")
                       or pending.get("kind") == "plan_approval" and answer in ("non", "n", "no"))
        orchestrator = orchestrator_factory() if orchestrator_factory else _create_orchestrator(require_configuration=not python_only)
        response = orchestrator.run(request, event_data=deepcopy(conversation["event_data"]),
                                    messages=conversation["messages"][:-1], interactive=True)
        if not isinstance(response, str) or not response.strip():
            raise ValueError("Réponse finale absente.")
        data = orchestrator.event_data
        refused = ((data.get("plan_approval") or {}).get("approved") is False
                   or any(item.get("approved") is False for item in data.get("documents", []))
                   or any(item.get("status") == "refused" for item in data.get("tasks", {}).values()))
        conversation["status"] = ("waiting" if data.get("pending_input") else "refused" if refused else
                                  {"success": "completed", "partial": "partial"}.get(orchestrator.last_status, "error"))
    except (KeyboardInterrupt, EOFError):
        conversation["status"] = "interrupted"
        response = "Exécution interrompue. Les décisions déjà enregistrées sont conservées."
    except Exception as error:
        conversation["status"] = "error"
        # Les exceptions de fournisseur peuvent contenir des données techniques sensibles.
        response = (str(error) if isinstance(error, ConversationError) else
                    f"Exécution interrompue ({type(error).__name__}). Les décisions déjà enregistrées sont conservées.")
    conversation["messages"].append({"role": "assistant", "content": response})
    if orchestrator is not None:
        conversation["event_data"] = deepcopy(getattr(orchestrator, "event_data", conversation["event_data"]))
        conversation["event_details"] = deepcopy(getattr(orchestrator, "event_details", conversation.get("event_details", {})))
    try:
        save_conversation(conversation, directory)
    except ConversationError as error:
        error.conversation = conversation
        raise
    return conversation
