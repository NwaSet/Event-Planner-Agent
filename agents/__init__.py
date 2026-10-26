"""Factories for event specialists using the existing Agent execution model.

Construction performs no data reads, LLM requests, or persistence. Each factory
returns a fresh Agent with its own conversation histories and tool schemas.
The caller supplies context from other specialists; no workflow is imposed.

Dataset limitations: venues and caterers are fictional; technical provider kits
and prices are simulated. There are no live availability calendars or verified
email contacts. Monetary tools sum full line amounts on one tax basis; schedule
tools check structure and declared exclusive resources, not business availability.

Use run(request, conversation_id="conv_001") for a conversation, and as_tool()
for supervisor delegation. A future controller can explicitly persist histories
with MemoryDAO.save_memory(conversation_id, agent.name, agent.get_history(...))
and restore them with agent.load_history(..., conversation_id=...). These
factories neither register persistence listeners nor save memories themselves.
"""

from agents.budget_agent import create_budget_agent
from agents.provider_agent import create_provider_agent
from agents.schedule_agent import create_schedule_agent
from agents.venue_agent import create_venue_agent
from agents.writer_agent import create_writer_agent

__all__ = [
    "create_budget_agent",
    "create_venue_agent",
    "create_provider_agent",
    "create_schedule_agent",
    "create_writer_agent",
]
