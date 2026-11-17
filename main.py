"""Point d'entrée terminal : une demande, une exécution, puis arrêt."""

from model import llm, trace
from model.event_orchestrator import create_orchestrator


def main() -> int:
    try:
        trace.title("Event Planner Agent")

        probleme = llm.verifier_configuration()
        if not llm.BASE_URL.strip() or not llm.MODELE.strip():
            probleme = "Renseignez LLM_BASE_URL et LLM_MODEL dans le fichier .env."
        if probleme:
            trace.surrond(probleme, "Configuration du LLM", "red")
            return 1

        trace.surrond(
            "Décrivez votre événement, puis appuyez sur Entrée.\n"
            "Une seule demande sera traitée. Aucun historique de conversation n'est repris.\n"
            "Si un email est préparé, sa validation vous sera demandée dans ce terminal.",
            "Votre demande",
            "cyan",
        )
        demande = trace.console.input("Votre événement > ").strip()
        if not demande:
            trace.tracer("Application", "INFO", "Demande vide : aucune exécution.")
            return 0

        llm.nouvelle_demande()
        orchestrateur = create_orchestrator()
        trace.tracer("Application", "INFO", "Traitement de votre demande en cours...")
        reponse = orchestrateur.run(demande)
        trace.surrond(reponse or "L'orchestrateur n'a retourné aucun texte.", "Résultat", "green")
        return 0

    except EOFError:
        trace.tracer("Application", "INFO", "Saisie fermée : arrêt du programme.")
        return 0
    except KeyboardInterrupt:
        trace.tracer("Application", "INFO", "Exécution interrompue par l'utilisateur.")
        return 130
    except llm.ErreurLLM as erreur:
        trace.surrond(str(erreur), "Erreur du LLM", "red")
        return 1
    except Exception as erreur:
        trace.surrond(f"{type(erreur).__name__} : {erreur}", "Erreur d'exécution", "red")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
