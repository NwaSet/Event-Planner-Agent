"""
fonction qui permet de créer chaque agent ia.
"""
from ..agent import Agent

from .prompt import PROMPT_BUDGETARY, PROMPT_WRITER, PROMPT_PLANNING, PROMPT_DECISION_MAKING

from .tools import get_meteo
from .tools import SCHEMA_METEO
from .tools import get_caterers, get_providers, get_venues
from .tools import SCHEMA_CATERERS, SCHEMA_PROVIDERS, SCHEMA_VENUES

def create_budgetary_agent() -> Agent:
    return Agent(
        name = "budgetary agent",
        instructions = PROMPT_BUDGETARY
    )

def create_writer_agent() -> Agent:
    return Agent(
        name = "writer agent",
        instructions= PROMPT_WRITER
    )

def create_planning_agent() -> Agent:
    return Agent(
        name = "planning agent",
        instructions = PROMPT_PLANNING,
        tools= (get_meteo, SCHEMA_METEO)
    )

def create_decision_making_agent() -> Agent:
    return Agent(
        name = "decision making agent",
        instructions = PROMPT_DECISION_MAKING,
        tools= [(get_caterers, SCHEMA_CATERERS), (get_providers, SCHEMA_PROVIDERS), (get_venues, SCHEMA_VENUES)]
    )
