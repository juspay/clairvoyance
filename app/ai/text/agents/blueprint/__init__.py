"""Blueprint — single-agent template creation/edit via chat.

Public contract:
- ``create_blueprint_agent(checkpointer=...)`` — compiled LangGraph.

Everything else is internal to the package.
"""

from app.ai.text.agents.blueprint.agent.graph import create_blueprint_agent

__all__ = ["create_blueprint_agent"]
