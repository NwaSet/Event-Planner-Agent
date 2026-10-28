"""
display in the terminal the traces made by an agent using Rich library. 

each row will look like this :
    AGENT NAME [ACTION] : summury

- AGENT NAME will always have the same color.
- all ACTION have a different color.

an external listenner car take the informations made by the trace to do an interface (streamlite)
"""

# IMPORTS
import json
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()
_listenner = None
_colors_agents = {}

# CONST
COLORS_TYPES = {
    "ASK": "bold white",
    "THINK": "yellow",
    "ACTION": "cyan",
    "OBSERV": "green",
    "RESPONSE": "bold magenta",
    "LLM": "dim",
    "HUMAN": "bold red",
    "INFO": "blue",
}

AGENTS_PALETTE = ["bright_blue", "bright_magenta", "orange1", "bright_cyan",
                  "bright_green", "deep_pink2", "gold1", "medium_purple1"]


def define_listenner(fonction : function) -> None:
    """save a function to execute in all other function"""
    global _listenner
    _listenner = fonction

def agent_color(agent : str) -> None :
    if agent not in _colors_agents :
        index = len(_colors_agents) % len(AGENTS_PALETTE)
        _colors_agents[agent] = AGENTS_PALETTE[index]

    return _colors_agents[agent]

def reduce_text(text : str, max_length : int = 400) -> str:
    text = str(text).strip()
    if len(text) > max_length :
        return text[:max_length] + ["..."]
    return text

def tracer(agent, type_evenement, text, **details) :
    ligne = Text()
    ligne.append(f"{agent:<18}", style=f"bold {agent_color(agent)}")
    ligne.append(f" [{type_evenement}] ", style=COLORS_TYPES.get(type_evenement, "white"))
    # Text.append n'interprète pas les crochets : pas de risque avec du JSON
    style_texte = "dim" if type_evenement == "LLM" else ""
    # Les résultats d'outils peuvent être très longs : on les coupe à l'affichage
    ligne.append(reduce_text(text) if type_evenement == "OBSERVATION" else str(text), style=style_texte)
    console.print(ligne)

    if _listenner is not None:
        _listenner({
            "heure": datetime.now().strftime("%H:%M:%S"),
            "agent": agent,
            "type": type_evenement,
            "texte": str(text),
            **details,
        })

# ##### ##### ##### #####
#   most frequent event
# ##### ##### ##### #####

def think(agent, text) :
    tracer(agent, "THINk", text)

def action(agent, text) :
    tracer(agent, "ACTION", text)

def observation(agent, result) :
    if not isinstance(result, str) :
        result = json.dumps(result, ensure_ascii=False)
    tracer(agent, "OBSERV", result)

def response(agent, texte):
    tracer(agent, "RESPONSE", texte)

def title(texte):
    console.rule(f"[bold]{texte}")

def surrond(text, title_surrond="", color="white") :
    console.print(
        Panel(
            Text(
                str(text)
            ), 
            title=title_surrond,
            border_style=color
        )
    )