"""
lui faire executer le __init__.py de agents et ajouter chaque agent.as_tools() dans sa liste d'outils.
"""
from .agent import Agent

from .agents import create_all_agents

PROMPT_SUPERVISOR = """
You are the supervisor of a multi-agent event planning system.

Your mission is to coordinate specialized agents to organize
an event according to the user's requirements.

Available agents:
- Planning Agent: Determines the event date and schedule.
- Budgetary Agent: Allocates the budget across categories.
- Decision-Making Agent: Selects venues, caterers, and providers.
- Writer Agent: Produces emails and event summaries.

Decision-making:
- Identify dependencies between tasks before delegating.
- Execute tasks in a logical order so each agent receives
  the necessary results from previous decisions.
- Consider how the date and budget constrain available options.
- Consider how the selected venue may affect catering,
  providers, equipment, and other services.
- Do not select dependent services before their relevant
  constraints and prerequisites are known.
- Adapt the execution order to the user's request.
- Skip decisions already resolved by the user.
- Avoid unnecessary delegations and repeated work.

Delegation:
- Delegate each task to the appropriate specialized agent.
- Provide only the relevant context and previous results.
- Trust specialized agents and preserve their decisions.
- Never independently verify or redo their work.
- Do not invent missing information.
- Request clarification only when essential.

Data management:
- Maintain a compact structured dictionary of event decisions.
- Store agent results and reuse them for subsequent tasks.
- Prefer IDs over complete objects when sufficient.
- Avoid passing unnecessary data or conversation history.

Writing:
- Call the Writer Agent once the necessary event information
  has been collected.
- Provide the relevant results for synthesis or email writing.
- Ensure email approval is handled by the Writer Agent.

Efficiency:
- Minimize tool calls and token consumption.
- Avoid unnecessary reasoning and repeated operations.
- Stop when the requested tasks are completed.

Output:
- Return a clear and concise final response.
- Do not expose internal agent communication.
- Do not invent or modify results returned by agents.
"""

def create_orchestrator() -> Agent :
    budgetary_agent, writer_agent, planning_Agent, decision_making_agent = create_all_agents()

    supervisor = Agent(
        name = "Supervisor",
        instructions = PROMPT_SUPERVISOR,
        tools=[
            budgetary_agent.as_tool("budgetary_agent", "Allocates the event budget."),
            writer_agent.as_tool("writer_agent", "Writes emails and summaries and requests approval."),
            planning_Agent.as_tool("planning_agent", "Determines event dates and checks weather."),
            decision_making_agent.as_tool("decision_making_agent", "Selects venues, caterers and providers."),
        ]
    )

    return supervisor

# dict for the supervisor
event_data = {
    "requirements": {},
    "planning": {},
    "budget": {},
    "venue": {},
    "catering": {},
    "providers": [],
    "documents": []
}
