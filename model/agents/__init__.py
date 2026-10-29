"""
faire la fonction qui va créer chaque agents ia et les returns sous la forme d'une list / dictionnaire ? 
"""
from ..agent import Agent
from .agents import create_budgetary_agent, create_writer_agent, create_planning_agent, create_decision_making_agent

def create_all_agents() -> list[Agent, Agent, Agent, Agent] :
    budgetary_agent = create_budgetary_agent()
    writer_agent = create_writer_agent()
    planning_Agent = create_planning_agent()
    decision_making_agent = create_decision_making_agent()

    return [budgetary_agent, writer_agent, planning_Agent, decision_making_agent]