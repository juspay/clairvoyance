"""
Workflow Engine for Dynamic Flow Configuration

This package provides the core infrastructure for loading, building, and executing
dynamic conversation flows from database configurations.

NOTE: deliberately NO eager re-exports here. ``builder`` transitively imports
the handlers package (builtin_dispatcher -> query_knowledge_base -> the KB
runtime adapter), which itself imports ``template.types`` — an eager
``from .builder import FlowConfigBuilder`` in this file would make ANY import
of ``template.types`` (pure Pydantic models) re-enter this package and crash
with a circular ImportError depending on which module loads first. Import
from the submodules directly, e.g.::

    from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
    from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
"""
