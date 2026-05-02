"""Builder for the Blueprint :class:`TemplateSchemaGraph`.

``build_schema_graph()`` is the one entry point the rest of the package
(planner, specialists, conversation) should use. It introspects the
canonical Breeze Buddy template types, attaches known couplings, and
includes the sub-schemas the flow specialist needs.
"""

from functools import lru_cache

from app.ai.text.agents.blueprint.schema.couplings import COUPLINGS
from app.ai.text.agents.blueprint.schema.enrich import apply_enrichment
from app.ai.text.agents.blueprint.schema.introspect import introspect_template
from app.ai.text.agents.blueprint.schema.models import TemplateSchemaGraph
from app.ai.voice.agents.breeze_buddy.template.types import (
    FlowNodeModel,
    GlobalBuiltinFunction,
    GlobalHttpFunction,
    TemplateModel,
)


@lru_cache(maxsize=1)
def build_schema_graph() -> TemplateSchemaGraph:
    """Build the full schema graph once per process and cache it.

    The graph is pure data derived from static class definitions, so a
    single cached instance is safe to share across sessions.
    """
    fields, sub_schemas = introspect_template(
        TemplateModel,
        extra_sub_schemas=[
            FlowNodeModel,
            GlobalBuiltinFunction,
            GlobalHttpFunction,
        ],
    )
    # Merge hand-curated SPEC enrichment onto the introspected fields —
    # rationale, recommendation, example_phrasings. Missing entries are
    # a no-op (the fields keep their Pydantic-derived defaults).
    apply_enrichment(fields, sub_schemas)
    return TemplateSchemaGraph(
        root=TemplateModel.__name__,
        fields=fields,
        sub_schemas=sub_schemas,
        couplings=list(COUPLINGS),
    )


__all__ = ["build_schema_graph"]
