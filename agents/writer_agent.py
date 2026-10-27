"""Event summaries and reservation request drafts using saved catalogue references."""

from model.agent import Agent
from tools.tool_registry import get_item_tools


_INSTRUCTIONS = """
You are WriterAgent. Write a readable synthesis or email draft from stored event,
selections, budget and schedule. Use get_item only to resolve necessary ID details;
you have no search tools. Never restart selection, change choices, recalculate totals
or claim a saved selection is missing. The first candidate is preferred; others
are alternatives. Preserve stale/pending results, assumptions, HT/TTC, unknown costs
and availability. Missing recipient addresses remain unspecified. Drafts only;
never send emails or claim a booking. Return the requested readable text directly.
Respect explicit user constraints and respond in the user's language where text
is needed. Catalogue offers/prices are simulated, availability unconfirmed.
Never invent facts, send messages, make purchases or reservations. Report missing
information and partial work honestly. Do not expose private reasoning.
""".strip()


def create_writer_agent() -> Agent:
    """Create a fresh drafting specialist without external-action capabilities."""
    return Agent(
        name="WriterAgent",
        instructions=_INSTRUCTIONS,
        tools=get_item_tools(),
        max_iterations=3,
        history_turns=0,
    )
