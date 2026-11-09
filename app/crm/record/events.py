"""Attributed-event reads — owned by the record module, consumed by the
outreach walker (the goal re-check at fire time).

Trivial today — one query, one decode, no decisions — but contracts.py
re-exports from here rather than db/accessor directly (the timeline.py
seam): cross-module callers never depend on a mechanical accessor
signature.
"""

from datetime import datetime
from typing import Any, Collection, Dict, List, Optional, Tuple

from app.crm.record.catalog import catalog_fields
from app.crm.record.db import accessor
from app.crm.record.extractors.engine import LIST_TYPE, list_values, variable_name
from app.crm.record.schemas import RawEvent


async def customer_has_event(
    merchant_id: str,
    customer_id: str,
    topics: List[str],
    since: datetime,
    where: Optional[Tuple[str, str]] = None,
) -> bool:
    """Did she do one of ``topics`` after ``since``? ``where`` narrows it
    to the letters whose payload field equals a value (the goal key)."""
    return await accessor.customer_has_event(
        merchant_id, customer_id, topics, since, where
    )


async def latest_customer_event(
    merchant_id: str,
    customer_id: str,
    topics: List[str],
    since: datetime,
    where: Tuple[str, str],
) -> Optional[RawEvent]:
    """The newest of the letters customer_has_event would find, so a goal
    tier with conditions is judged at fire time on the order's state now."""
    return await accessor.latest_customer_event(
        merchant_id, customer_id, topics, since, where
    )


async def event_topics(merchant_id: str, event_ids: List[str]) -> Dict[str, str]:
    """{letter id: topic} for a few ids a run's trail points at — so the
    console can say WHICH event moved a square without outreach reading
    record's table (rule 12's one direction)."""
    return await accessor.event_topics(merchant_id, event_ids)


async def letters_by_external_ids(
    merchant_id: str, source: str, external_ids: List[str]
) -> List[RawEvent]:
    """The letters filed under these external ids — for the module that
    composed them: connectivity reads a message's receipts back once its row
    knows the provider's id, without reading record's table (rule 12's one
    direction)."""
    return await accessor.events_by_external_ids(merchant_id, source, external_ids)


async def declared_lists(
    merchant_id: str, event_id: str, names: Collection[str]
) -> Dict[str, Optional[List[Any]]]:
    """{name: the list itself} for each declared `list` variable of this
    letter's topic that ``names`` asks for — None when it is declared but
    this letter carries no list there, so the caller can tell "not a list
    field" (absent) from "a list field with nothing in it" (None).

    A run keeps only small scalar facts — a declared list reaches it as ONE
    line of text (engine.join_list), which is what a WhatsApp blank or a
    call can read. A channel that takes structured data (a Xyne form's
    options) needs the elements themselves, so they are read here, from
    the letter the run points at, through the same declared path and item
    filter the text was rendered from. Nothing is copied onto the run.

    Empty when the letter is gone or names nothing declared; raises
    AmbiguousTopic when two sources declare its topic (publish refuses such
    a door, so a live run meeting one is a catalog that changed since)."""
    wanted = set(names)
    if not wanted:
        return {}
    event = await accessor.get_event(merchant_id, event_id)
    if event is None:
        return {}
    fields = await catalog_fields(merchant_id, event.topic) or {}
    found: Dict[str, Optional[List[Any]]] = {}
    for path, field in fields.items():
        if field.type != LIST_TYPE or not field.variable or field.deprecated:
            continue
        name = variable_name(path)
        if name not in wanted:
            continue
        found[name] = list_values(event.payload, path, field.item_where or None)
    return found
