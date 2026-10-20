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
    global _ecouteur
    _ecouteur = fonction

def agent_color(agent : str) -> None :
    raise NotImplementedError("To Do")

def reduce_text(text : str, max_length : int = 400) :
    raise NotImplementedError("To Do")