"""Affichage en lecture seule d'une conversation enregistrée."""

import json

from rich.json import JSON
from rich.panel import Panel
from rich.text import Text


def display_conversation(conversation, console):
    console.print(Panel(Text(conversation["title"]), title="Conversation — lecture seule"))
    for message in conversation["messages"]:
        console.print(Panel(Text(message["content"]),
                            title="Vous" if message["role"] == "user" else "Résultat sauvegardé"))
    for key, title in (("event_data", "Événement enregistré"), ("event_details", "Sélections et montants enregistrés")):
        console.print(Panel(JSON(json.dumps(conversation.get(key, {}), ensure_ascii=False)), title=title))
    if conversation["status"] == "running":
        console.print("Exécution non finalisée : seuls les éléments déjà sauvegardés sont disponibles.")
