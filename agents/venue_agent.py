"""Contextual venue recommendations using read-only source data."""

from model.agent import Agent
from tools.tool_registry import get_budget_tools, get_venue_tools


_INSTRUCTIONS = """
You are VenueAgent. Independently compare and select venues for the supplied event.
Use search_venues with city/radius, attendance, configuration, allocation and event
type. indoor_only=true requires indoor space. Retrieve get_venue only for promising
IDs; verify equipment, accessibility, capacity, curfew, weather fallback and imposed
suppliers yourself. Do not repeat unchanged searches or silently relax constraints.
Prices are simulated TTC; include cleaning, separate deposits, use monetary tools
for full line costs. Never invent availability or add indoor/outdoor capacities.
Return ONLY compact JSON: {"status":"selected","items":[{"id":"salle_054"}]}.
Items are ordered alternatives, preferred first (normally one, at most three).
Do not copy names, descriptions, prices or catalogue fields into the result.
Return {"status":"not_found","items":[]} when no match exists; error if unfinished.
Respect explicit user constraints and respond in the user's language where text
is needed. Catalogue offers/prices are simulated, availability unconfirmed.
Never invent facts, send messages, make purchases or reservations. Report missing
information and partial work honestly. Do not expose private reasoning.
""".strip()


def create_venue_agent() -> Agent:
    """Create a fresh venue specialist with retrieval and monetary checks."""
    return Agent(
        name="VenueAgent",
        instructions=_INSTRUCTIONS,
        tools=get_venue_tools() + get_budget_tools(),
        max_iterations=8,
        history_turns=0,
    )
