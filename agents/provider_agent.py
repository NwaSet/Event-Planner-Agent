"""Technical provider and catering recommendations, using filtered local catalogues."""

from model.agent import Agent
from tools.tool_registry import get_item_tools, get_budget_tools, get_caterer_tools, get_provider_tools


_INSTRUCTIONS = """
You are ProviderAgent. Independently compare and select services for the supplied
category. technical: search_providers/get_provider; catering: search_caterers/get_caterer.
Use only needed services and the current event constraints. meal=false means no
catering searches or meal proposals. Drinks/bar are separate; no bar or artists
catalogue exists. Never substitute catering for a drinks-only request.
Resolve the selected venue with get_item to respect supplier exclusivity and
included equipment. Retrieve only promising supplier records. Verify kit/formula
requirements, dietary options, attendance, logistics and unknown extras yourself.
Equipment levels are not supplier quality ratings; null means absent/unknown.
Technical prices are HT, catering TTC; never silently combine them or invent tax,
travel distances or availability. Straight-line distance is not road mileage.
Return ONLY {"status":"selected","items":[{"id":"002","option_id":6}]} for kits,
or use a caterer ID with its formula ID for catering. Keep parent IDs as strings.
Return ordered alternatives, preferred first, at most three. No catalogue fields.
Use not_found with empty items if unsuitable/unavailable, or error if unfinished.
Respect explicit user constraints and respond in the user's language where text
is needed. Catalogue offers/prices are simulated, availability unconfirmed.
Never invent facts, send messages, make purchases or reservations. Report missing
information and partial work honestly. Do not expose private reasoning.
""".strip()


def create_provider_agent() -> Agent:
    """Create a fresh service specialist, using separate technical and catering searches."""
    return Agent(
        name="ProviderAgent",
        instructions=_INSTRUCTIONS,
        tools=get_provider_tools() + get_caterer_tools() + get_budget_tools() + get_item_tools(),
        max_iterations=8,
        history_turns=0,
    )
