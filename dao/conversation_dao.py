"""Conversations locales JSON ; aucune dépendance aux agents ou à l'interface."""

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


CONVERSATIONS_DIR = Path(__file__).resolve().parents[1] / "data" / "conversations"
STATUSES = {"running", "completed", "partial", "refused", "error", "interrupted"}


class ConversationError(Exception):
    """Erreur de stockage/format présentable à l'utilisateur."""

    def __init__(self, message, *, conversation=None):
        super().__init__(message)
        self.conversation = conversation  # résultat en mémoire si la sauvegarde finale échoue


def _now():
    return datetime.now(timezone.utc).isoformat()


def _path(conversation_id, directory=None):
    if not isinstance(conversation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", conversation_id):
        raise ConversationError("Identifiant de conversation invalide.")
    root = Path(directory) if directory is not None else CONVERSATIONS_DIR
    target = root / (conversation_id + ".json")
    if target.is_symlink() or target.resolve().parent != root.resolve():
        raise ConversationError("Chemin de conversation non autorisé.")
    return target


def _json_types(value):
    """Ne pas convertir silencieusement des objets ou des identifiants en chaînes."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Les clés JSON doivent être des chaînes.")
        for item in value.values():
            _json_types(item)
    elif isinstance(value, list):
        for item in value:
            _json_types(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError(f"Type non sérialisable : {type(value).__name__}.")


def _validate(conversation):
    try:
        if not isinstance(conversation, dict) or type(conversation.get("schema_version")) is not int or conversation["schema_version"] != 1:
            raise ValueError("Version de conversation incompatible (version 1 attendue).")
        if not isinstance(conversation["conversation_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", conversation["conversation_id"]):
            raise ValueError("Identifiant invalide.")
        if not isinstance(conversation["title"], str) or not conversation["title"].strip():
            raise ValueError("Titre manquant.")
        timestamps = [datetime.fromisoformat(conversation[key]) for key in ("created_at", "updated_at")]
        if any(value.utcoffset() is None for value in timestamps) or timestamps[1] < timestamps[0]:
            raise ValueError("Dates ISO 8601 avec fuseau et ordre chronologique attendues.")
        if conversation["status"] not in STATUSES:
            raise ValueError("Statut inconnu.")
        messages = conversation["messages"]
        if not isinstance(messages, list) or not messages or any(
                not isinstance(message, dict) or message.get("role") not in ("user", "assistant")
                or not isinstance(message.get("content"), str) for message in messages):
            raise ValueError("Historique de messages invalide.")
        if not isinstance(conversation["event_data"], dict) or not isinstance(conversation.get("event_details", {}), dict):
            raise ValueError("État événement invalide.")
        _json_types(conversation)
        json.dumps(conversation, allow_nan=False)  # rejette aussi NaN/Infinity, sans default=str
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise ConversationError(f"Format de conversation invalide : {error}") from error


def create_conversation(request):
    """Créer un enregistrement indépendant en mémoire, sans LLM ni écriture."""
    if not isinstance(request, str) or not request.strip():
        raise ConversationError("La demande doit être un texte non vide.")
    now = _now()
    title = " ".join(request.split())
    return {"schema_version": 1, "conversation_id": uuid4().hex,
            "title": title if len(title) <= 80 else title[:77] + "...",
            "created_at": now, "updated_at": now, "status": "running",
            "messages": [{"role": "user", "content": request}], "event_data": {}, "event_details": {}}


def save_conversation(conversation, directory=None):
    """Écrire dans un fichier temporaire voisin puis remplacer atomiquement le JSON."""
    _validate(conversation)
    saved = {**conversation, "updated_at": _now()}
    _validate(saved)
    temporary = None
    try:
        target = _path(saved["conversation_id"], directory)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=target.parent,
                                         prefix=".conversation-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(saved, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        conversation["updated_at"] = saved["updated_at"]
        return target
    except OSError as error:
        raise ConversationError("Impossible d'écrire la conversation ; vérifiez le dossier et ses permissions.") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # le JSON précédent reste intact même si le nettoyage du temporaire échoue


def load_conversation(conversation_id, directory=None):
    """Charger des données uniquement, sans agents, calculs ni modification de fichier."""
    try:
        target = _path(conversation_id, directory)
        with target.open(encoding="utf-8") as stream:
            conversation = json.load(stream)
    except FileNotFoundError as error:
        raise ConversationError("Conversation introuvable.") from error
    except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
        raise ConversationError("Fichier de conversation corrompu : JSON illisible.") from error
    except OSError as error:
        raise ConversationError("Impossible de lire la conversation.") from error
    _validate(conversation)
    if conversation["conversation_id"] != conversation_id:
        raise ConversationError("L'identifiant du fichier ne correspond pas à la conversation.")
    return conversation


def list_conversations(directory=None):
    """Métadonnées triées par création décroissante et diagnostics des fichiers illisibles."""
    root = Path(directory) if directory is not None else CONVERSATIONS_DIR
    entries, errors = [], []
    try:
        files = list(root.iterdir()) if root.exists() else []
    except OSError as error:
        raise ConversationError("Impossible de consulter le dossier des conversations.") from error
    for path in files:
        if path.suffix != ".json":
            continue
        try:
            conversation = load_conversation(path.stem, root)
            entries.append({key: conversation[key] for key in
                            ("conversation_id", "title", "created_at", "updated_at", "status")})
        except ConversationError as error:
            errors.append({"file": path.name, "error": str(error)})
    entries.sort(key=lambda entry: (datetime.fromisoformat(entry["created_at"]), entry["conversation_id"]), reverse=True)
    return {"conversations": entries, "errors": errors}
