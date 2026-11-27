"""Menu terminal : nouvelles exécutions indépendantes et historique en lecture seule."""

import argparse
import subprocess
import sys
from pathlib import Path

from rich.table import Table
from rich.text import Text

from controller.event_controller import run_conversation, continue_conversation
from dao.conversation_dao import ConversationError, list_conversations, load_conversation
from model import trace
from view.view import display_conversation


STATUS_LABELS = {"running": "Non finalisée", "completed": "Terminée", "partial": "Partielle",
                 "refused": "Validation refusée", "error": "Erreur", "interrupted": "Interrompue", "waiting": "Votre réponse est attendue"}
EXIT_CODES = {"completed": 0, "partial": 2, "refused": 2, "error": 1, "interrupted": 130, "waiting": 0}


def show_result(conversation):
    status = conversation["status"]
    color = "green" if status == "completed" else "red" if status == "error" else "yellow"
    trace.surrond(conversation["messages"][-1]["content"], STATUS_LABELS[status], color)
    state = conversation["event_data"]
    pending = state.get("workflow", state).get("pending_input")
    if pending and pending["kind"] in ("plan_approval", "email_approval"):
        trace.console.print("Validez-vous ce plan ? (oui/non)" if pending["kind"] == "plan_approval" else
                            "Validez-vous ce brouillon exact ? (oui/non)")


def show_history(directory=None):
    history = list_conversations(directory)
    for error in history["errors"]:
        trace.surrond(error["file"] + " : " + error["error"], "Conversation illisible", "yellow")
    entries = history["conversations"]
    if not entries:
        trace.surrond("Aucune conversation consultable.", "Historique", "cyan")
        return 0
    table = Table(title="Conversations précédentes")
    for label in ("N°", "Titre", "Création", "Statut", "Identifiant"):
        table.add_column(label)
    for index, entry in enumerate(entries, 1):
        table.add_row(str(index), Text(entry["title"]), entry["created_at"],
                      STATUS_LABELS[entry["status"]], entry["conversation_id"])
    trace.console.print(table)
    while True:
        choice = trace.console.input("Conversation à consulter (0 = retour) > ").strip()
        if choice == "0":
            return 0
        if choice.isdecimal() and 1 <= int(choice) <= len(entries):
            conversation = load_conversation(entries[int(choice) - 1]["conversation_id"], directory)
            display_conversation(conversation, trace.console)
            return conversation_loop(conversation, directory)
        trace.surrond("Choisissez un numéro de la liste ou 0.", "Choix invalide", "yellow")


def conversation_loop(conversation, directory=None, exit_code=0):
    trace.surrond(conversation["title"], "Conversation — Quitter ou Entrée pour revenir au menu", "cyan")
    state = conversation["event_data"]
    pending = state.get("workflow", state).get("pending_input")
    if pending:
        trace.surrond(pending["content"], "Question ou validation en attente", "yellow")
    while True:
        request = trace.console.input("Vous > ")
        if not request.strip() or request.strip().casefold().rstrip(".! ") in ("quitter", "/quitter"):
            return exit_code
        try:
            conversation = continue_conversation(conversation, request, directory=directory)
            show_result(conversation)
            exit_code = EXIT_CODES[conversation["status"]]
            if exit_code == 130:
                return exit_code
        except ConversationError as error:
            if error.conversation is not None:
                conversation = error.conversation
                show_result(conversation)
            trace.surrond(str(error), "Échange non sauvegardé", "red")
            exit_code = 1


def main(*, directory=None) -> int:
    exit_code = 0
    try:
        while True:
            trace.title("Event Planner Agent")
            trace.console.print("1. Nouvelle conversation\n2. Historique des conversations\n3. Quitter")
            choice = trace.console.input("Votre choix > ").strip()
            if choice == "3":
                return exit_code
            try:
                if choice == "2":
                    exit_code = show_history(directory)
                elif choice == "1":
                    trace.surrond("Décrivez votre événement. Cette nouvelle demande sera sauvegardée.\n"
                                  "Les validations du plan et des emails restent demandées dans le terminal.",
                                  "Nouvelle conversation", "cyan")
                    request = trace.console.input("Votre événement > ")
                    if not request.strip():
                        trace.surrond("Demande vide : aucune exécution.", "Nouvelle conversation", "yellow")
                        continue
                    conversation = run_conversation(request, directory=directory)
                    show_result(conversation)
                    trace.surrond(conversation["conversation_id"], "Conversation sauvegardée", "cyan")
                    exit_code = EXIT_CODES[conversation["status"]]
                    if exit_code == 130:
                        return exit_code
                    exit_code = conversation_loop(conversation, directory, exit_code)
                else:
                    trace.surrond("Choisissez 1, 2 ou 3.", "Choix invalide", "yellow")
            except ConversationError as error:
                if error.conversation is not None:
                    show_result(error.conversation)
                trace.surrond(str(error), "Erreur de conversation — sauvegarde/lecture non confirmée", "red")
                exit_code = 1
    except EOFError:
        trace.tracer("Application", "INFO", "Saisie fermée : arrêt du programme.")
        return exit_code
    except KeyboardInterrupt:
        trace.tracer("Application", "INFO", "Exécution interrompue par l'utilisateur.")
        return 130


def launch(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Event Planner Agent — console Rich")
    parser.add_argument("--current-console", action="store_true",
                        help="Utiliser le terminal actuel au lieu d'ouvrir une nouvelle console Windows.")
    options = parser.parse_args(argv)
    if sys.platform == "win32" and not options.current_console:
        script = Path(__file__).resolve()
        try:
            # Même interpréteur/venv ; le marqueur évite une ouverture récursive de fenêtres.
            return subprocess.run([sys.executable, str(script), "--current-console"],
                                  cwd=script.parent, creationflags=subprocess.CREATE_NEW_CONSOLE).returncode
        except OSError:
            trace.surrond("Impossible d'ouvrir la console Windows. Relancez avec --current-console.",
                          "Erreur de lancement", "red")
            return 1
        except KeyboardInterrupt:
            return 130
    return main()


if __name__ == "__main__":
    raise SystemExit(launch())
