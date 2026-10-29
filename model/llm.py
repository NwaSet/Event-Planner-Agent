import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import openai
from dotenv import load_dotenv
from rich.markup import escape
from rich.table import Table

from model import traces

# --- Configuration ----------------------------------------------------------

RACINE = Path(__file__).resolve().parent.parent
load_dotenv(RACINE / ".env")

BASE_URL = os.getenv("LLM_BASE_URL", "")
API_KEY = os.getenv("LLM_API_KEY", "")
MODELE = os.getenv("LLM_MODEL", "")

FICHIER_TRACES = RACINE / "data/logs/logs.jsonl"
JOURNAL = []                                 # un dictionnaire par appel au modèle
ATTENTES_429 = [2, 5, 10, 20, 30, 60]        # secondes d'attente entre les essais si 429

# Identifiant de la "demande utilisateur" en cours (pour calculer un coût par demande)
demande_en_cours = uuid.uuid4().hex[:8]


class ErreurLLM(Exception):
    """Erreur de configuration ou d'appel au LLM, avec un message clair en français."""


def nouvelle_demande():
    """À appeler au début de chaque nouvelle demande de l'utilisateur."""
    global demande_en_cours
    demande_en_cours = uuid.uuid4().hex[:8]
    return demande_en_cours


def verifier_configuration():
    """Renvoie None si la configuration semble correcte, sinon un message d'erreur."""
    en_local = "localhost" in BASE_URL or "127.0.0.1" in BASE_URL
    if not en_local and (not API_KEY or "collez-votre-cle" in API_KEY):
        return ("Aucune clé API trouvée.\n"
                "  1. Copiez le fichier .env.example en .env (dans le dossier code/)\n"
                "  2. Collez votre clé dans LLM_API_KEY=...\n"
)
    return None


_client = None


def client():
    """Crée le client une seule fois (on vérifie d'abord la configuration)."""
    global _client
    if _client is None:
        probleme = verifier_configuration()
        if probleme:
            raise ErreurLLM(probleme)
        _client = openai.OpenAI(
            base_url=BASE_URL,
            api_key=API_KEY or "ollama",
            max_retries=0,
            timeout=120,
        )
    return _client


# --- Appel au modèle --------------------------------------------------------

def chat(messages, tools=None, agent="?", temperature=0.3):
    """
    Envoie la conversation `messages` au modèle et renvoie son message de réponse.

    - tools : liste de schémas d'outils (format OpenAI), ou None
    - agent : nom de l'agent qui fait l'appel (pour les traces et les coûts)

    Le message renvoyé a deux attributs utiles :
        .content     -> le texte de la réponse (peut être vide)
        .tool_calls  -> la liste des outils que le modèle veut appeler (ou None)
    """
    parametres = {"model": MODELE, "messages": messages, "temperature": temperature}
    if tools:
        parametres["tools"] = tools
        parametres["tool_choice"] = "auto"

    for essai in range(len(ATTENTES_429) + 1):
        debut = time.time()
        try:
            reponse = client().chat.completions.create(**parametres)
            break
        except openai.RateLimitError:
            if essai == len(ATTENTES_429):
                raise ErreurLLM("Limite de débit (erreur 429) toujours atteinte après plusieurs essais.\n"
                                "  Attendez une minute, ou changez de fournisseur dans le .env.")
            attente = ATTENTES_429[essai]
            traces.tracer(agent, "INFO", f"Erreur 429 (trop de requêtes) : nouvelle tentative dans {attente} s...")
            time.sleep(attente)
        except openai.AuthenticationError:
            raise ErreurLLM("Clé API refusée (erreur 401).\n"
                            "  Vérifiez LLM_API_KEY dans le fichier .env (pas d'espace, pas de guillemets)\n"
                            "  et que la clé correspond bien au fournisseur de LLM_BASE_URL.") from None
        except openai.PermissionDeniedError:
            raise ErreurLLM("Accès refusé (erreur 403) : votre compte n'a pas accès à ce modèle\n"
                            f"  ({MODELE}). Vérifiez LLM_MODEL ou les droits de votre plan.") from None
        except openai.NotFoundError:
            raise ErreurLLM(f"Modèle ou adresse introuvable (erreur 404) : modèle '{MODELE}' sur {BASE_URL}.\n"
                            "  Vérifiez LLM_MODEL et LLM_BASE_URL. Avec Ollama : ollama pull <modèle>.") from None
        except openai.APIConnectionError:
            raise ErreurLLM(f"Impossible de joindre {BASE_URL}.\n"
                            "  Vérifiez votre connexion internet (ou, avec Ollama, que 'ollama serve' tourne).") from None
        except openai.BadRequestError as e:
            # Groq refuse lui-même un appel d'outil invalide (nom d'outil inventé...) : on redemande
            if "tool_use_failed" in str(e) and essai < len(ATTENTES_429):
                traces.tracer(agent, "INFO", "Appel d'outil invalide refusé par le fournisseur : nouvelle tentative...")
                continue
            raise ErreurLLM(f"Requête refusée par le fournisseur (erreur 400) :\n  {e}\n"
                            "  Si le message parle de 'tools' : ce modèle ne sait pas appeler d'outils,\n"
                            "  choisissez-en un autre dans le .env.") from None

    duree = time.time() - debut
    _enregistrer_usage(reponse, agent, duree)
    return reponse.choices[0].message


def _enregistrer_usage(reponse, agent, duree):
    """Note les tokens consommés dans JOURNAL et dans traces.jsonl."""
    usage = reponse.usage
    entree = {
        "horodatage": datetime.now().isoformat(timespec="seconds"),
        "demande": demande_en_cours,
        "agent": agent,
        "modele": MODELE,
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "duree_s": round(duree, 2),
    }
    JOURNAL.append(entree)
    try:
        with open(FICHIER_TRACES, "a", encoding="utf-8") as fichier:
            fichier.write(json.dumps(entree, ensure_ascii=False) + "\n")
    except OSError:
        pass  # pas grave si le fichier ne peut pas être écrit

    traces.tracer(agent, "LLM",
                  f"appel au modèle : {entree['prompt_tokens']} tokens envoyés, "
                  f"{entree['completion_tokens']} reçus, {entree['duree_s']} s",
                  prompt_tokens=entree["prompt_tokens"],
                  completion_tokens=entree["completion_tokens"])


def tool_schema(nom, description, proprietes, requis=None):
    """
    Construit la description d'un outil au format attendu par le modèle.
    proprietes : {"ville": {"type": "string", "description": "..."}, ...}
    requis     : liste des paramètres obligatoires (par défaut : tous)
    """
    return {
        "type": "function",
        "function": {
            "name": nom,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": proprietes,
                "required": list(proprietes) if requis is None else requis,
            },
        },
    }


def lire_arguments(appel):
    """Transforme les arguments d'un appel d'outil (texte JSON) en dictionnaire."""
    try:
        arguments = json.loads(appel.function.arguments or "{}")
        return arguments if isinstance(arguments, dict) else {}
    except json.JSONDecodeError:
        return {}


def message_assistant(message):
    """Convertit le message du modèle en dictionnaire à remettre dans l'historique."""
    resultat = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        resultat["tool_calls"] = [
            {"id": appel.id, "type": "function",
             "function": {"name": appel.function.name, "arguments": appel.function.arguments}}
            for appel in message.tool_calls
        ]
    return resultat


def tool_message(appel, resultat):
    """Message qui renvoie au modèle le résultat d'un outil (tool_call_id ET name : certains fournisseurs, dont Mistral, exigent les deux)."""
    if not isinstance(resultat, str):
        resultat = json.dumps(resultat, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": appel.id, "name": appel.function.name, "content": resultat}

# --- Coûts ------------------------------------------------------------------

def resume_couts(prix_input_par_million=0.10, prix_output_par_million=0.50): # prix d'open ai avec GPT-6 Luna
    """
    Affiche les tokens consommés depuis le lancement du script, agent par agent,
    et le coût estimé. Prix en dollars par million de tokens (par défaut : prix de
    qwen3.8-27b chez Groq, à vérifier sur le site du fournisseur).
    Renvoie le coût total estimé.
    """
    par_agent = {}
    for entree in JOURNAL:
        stats = par_agent.setdefault(entree["agent"], {"appels": 0, "in": 0, "out": 0})
        stats["appels"] += 1
        stats["in"] += entree["prompt_tokens"]
        stats["out"] += entree["completion_tokens"]

    tableau = Table(title=f"Consommation (modèle : {MODELE})")
    for colonne in ["Agent", "Appels", "Tokens entrée", "Tokens sortie", "Coût estimé ($)"]:
        tableau.add_column(colonne, justify="left" if colonne == "Agent" else "right")

    total = {"appels": 0, "in": 0, "out": 0, "cout": 0.0}
    for agent, stats in par_agent.items():
        cout = (stats["in"] * prix_input_par_million + stats["out"] * prix_output_par_million) / 1_000_000
        tableau.add_row(agent, str(stats["appels"]), str(stats["in"]), str(stats["out"]), f"{cout:.6f}")
        total["appels"] += stats["appels"]
        total["in"] += stats["in"]
        total["out"] += stats["out"]
        total["cout"] += cout
    tableau.add_row("TOTAL", str(total["appels"]), str(total["in"]), str(total["out"]),
                    f"{total['cout']:.6f}", style="bold")
    traces.console.print(tableau)
    return total["cout"]


# --- Messages d'erreur lisibles ----------------------------------------------

def _afficher_erreur(type_erreur, erreur, pile):
    """Remplace la longue pile d'erreur Python par un message clair pour nos erreurs."""
    if isinstance(erreur, ErreurLLM):
        traces.console.print(f"\n[bold red]Problème avec le LLM :[/] {escape(str(erreur))}")
    elif isinstance(erreur, NotImplementedError):
        traces.console.print(f"\n[bold yellow]À compléter :[/] {escape(str(erreur))}")
    elif isinstance(erreur, KeyboardInterrupt):
        traces.console.print("\n[dim]Interrompu.[/]")
    else:
        sys.__excepthook__(type_erreur, erreur, pile)


sys.excepthook = _afficher_erreur
