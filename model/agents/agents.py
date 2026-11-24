"""
fonction qui permet de créer chaque agent ia.
"""
from ..agent import Agent

from .prompt import PROMPT_BUDGETARY, PROMPT_WRITER, PROMPT_PLANNING, PROMPT_DECISION_MAKING

from .tools import get_meteo, ask_user, get_next_day
from .tools import SCHEMA_METEO, SCHEMA_ASK_USER, SCHEMA_NEXT_DAY
from .search import compact_tool

def create_budgetary_agent() -> Agent:
    return Agent(
        name = "budgetary agent",
        instructions = PROMPT_BUDGETARY
    )

def create_writer_agent() -> Agent:
    return Agent(
        name = "writer agent",
        instructions= PROMPT_WRITER,
        tools = [(ask_user, SCHEMA_ASK_USER)]
    )

def create_planning_agent() -> Agent:
    return Agent(
        name = "planning agent",
        instructions = PROMPT_PLANNING,
        tools= [(get_meteo, SCHEMA_METEO), (get_next_day, SCHEMA_NEXT_DAY)]
    )

def create_decision_making_agent() -> Agent:
    return Agent(
        name = "decision making agent",
        instructions = PROMPT_DECISION_MAKING,
        tools=[compact_tool(category) for category in ("catering", "provider", "venue")]
    )
