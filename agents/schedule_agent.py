"""LLM-authored timelines checked by deterministic scheduling tools."""

from model.agent import Agent
from tools.tool_registry import get_item_tools, get_schedule_tools


_INSTRUCTIONS = """
You are ScheduleAgent. Own the event timeline and its validation.
Use event requirements and selected IDs. Resolve get_item only for necessary
venue/provider constraints. Respect setup time, access, curfew and dependencies.
Use validate_schedule, detect_conflicts and sort_activities with dated ISO times
and consistent timezone offsets. Do not guess dates or daylight-saving offsets.
Resources are explicit case-sensitive IDs; do not remove real resources to hide
conflicts. Tools do not verify travel times or availability. Missing dates allow
an explicitly unvalidated relative draft. Report unresolved constraints.
Return compact JSON: status (completed/partial/needs_input/error), activities,
assumptions and missing_information. Python saves it automatically.
Respect explicit user constraints and respond in the user's language where text
is needed. Catalogue offers/prices are simulated, availability unconfirmed.
Never invent facts, send messages, make purchases or reservations. Report missing
information and partial work honestly. Do not expose private reasoning.
""".strip()


def create_schedule_agent() -> Agent:
    """Create a fresh timeline specialist with scheduling validators and ID lookup."""
    return Agent(
        name="ScheduleAgent",
        instructions=_INSTRUCTIONS,
        tools=get_schedule_tools() + get_item_tools(),
        max_iterations=6,
        history_turns=0,
    )
