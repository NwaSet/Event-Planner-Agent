"""
lui faire executer le __init__.py de agents et ajouter chaque agent.as_tools() dans sa liste d'outils.
"""
from .agent import Agent

from agents.__init__ import create_all_agents

PROMPT_SUPERVISOR = """

"""

def create_orchestrator() -> Agent :
    budgetary_agent, writer_agent, planning_Agent, decision_making_agent = create_all_agents()

    supervisor = Agent(
        name = "Supervisor",
        instructions = PROMPT_SUPERVISOR,
        tools=[budgetary_agent.as_tool(), writer_agent.as_tool(), planning_Agent.as_tool(), decision_making_agent.as_tool()]
    )

    return supervisor