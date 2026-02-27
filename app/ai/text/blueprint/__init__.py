"""Blueprint text agent for template generation."""

__all__ = ["create_blueprint_agent"]


def create_blueprint_agent(*args, **kwargs):
    """Lazy wrapper to avoid importing deepagents at module load time."""
    from app.ai.text.blueprint.agent import create_blueprint_agent as _create

    return _create(*args, **kwargs)
