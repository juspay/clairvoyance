"""Tools the BROWSER executes, not the server.

An ordinary tool runs inside the turn: the agent calls it, we await it, the
result goes back into the context. A client tool cannot — the thing it reads
(the page the shopper is looking at) only exists in their browser. So the turn
has to stop, the call has to travel to the widget, and a later request has to
carry the answer back.

That machine already exists. A HITL approval ends the turn, persists the call
as a PENDING ``tool_approvals`` row, and resumes on a separate endpoint with a
result the agent never computed. A client tool is the same gate with a
different answer: an approval's is a decision, a client tool's is the payload
the browser produced. So this module contributes the tool surface and the
predicate, and the gate in ``agent/cycle.py`` treats both as one case — no
second pause/resume path, and expiry, superseding and dangling-row cleanup
come along already written.

Gated ON THE TEMPLATE, by name: ``configurations.client_tools`` lists the ones
a template offers. The list IS the switch — a tool nobody can execute must not
be in the schema, so there is no second flag to drift out of step with it.
"""

from typing import Any, Dict, FrozenSet, Iterable

from pipecat_flows import FlowResult, FlowsFunctionSchema

from app.core.logger import logger

GET_CURRENT_PAGE_PRODUCT_TOOL_NAME = "get_current_page_product"

#: Every client tool the engine knows how to build. A name outside this set is
#: dropped and logged rather than published — see :func:`enabled_client_tools`.
KNOWN_CLIENT_TOOLS: FrozenSet[str] = frozenset({GET_CURRENT_PAGE_PRODUCT_TOOL_NAME})

#: How long a pending client-tool call stays answerable.
#:
#: Short, because the browser answers in milliseconds when it is there at all.
#: What this really bounds is the tab that closed, crashed or was backgrounded
#: mid-call: past it the row expires, the agent is told the read failed, and it
#: replies without the page. Long enough to survive a slow first paint; short
#: enough that a shopper who reopens the panel is not waiting on a dead call.
CLIENT_TOOL_EXPIRY_SECS = 60


def enabled_client_tools(configurations: Any) -> FrozenSet[str]:
    """The client tools this template offers, filtered to the ones we build.

    An unknown name is dropped rather than fatal — a typo should cost a
    template its page awareness, not its whole agent. It is logged because the
    failure is otherwise invisible: the tool never reaches the schema, the
    agent never calls it, and nothing anywhere says why.
    """
    names: Iterable[str] = getattr(configurations, "client_tools", None) or []
    enabled = frozenset(n for n in names if n in KNOWN_CLIENT_TOOLS)
    unknown = sorted(set(names) - enabled)
    if unknown:
        logger.warning(
            f"[client_tools] unknown client tool(s) ignored: {unknown}; "
            f"known: {sorted(KNOWN_CLIENT_TOOLS)}"
        )
    return enabled


async def _never_executes(_args: Dict[str, Any]) -> FlowResult:
    """Defensive: a client tool must be intercepted before dispatch.

    Reaching this means the gate in ``agent/cycle.py`` stopped recognising the
    call — the LLM would otherwise get a plausible-looking empty result and
    answer about a page nobody read. Failing loudly here turns that into one
    stack trace instead of a class of quiet wrong answers.
    """
    raise RuntimeError(
        "client tool reached server-side dispatch; the browser gate in "
        "agent/cycle.py should have intercepted it"
    )


def build_get_current_page_product_schema() -> FlowsFunctionSchema:
    """``get_current_page_product`` — which item the shopper is looking at.

    Returns an IDENTITY, not a description: ``product_id`` in the form the
    catalog tools take, plus the page url and title. Price, stock, variants
    and media are deliberately absent — those come from the catalogue, which
    is the merchant's own data, where a page is editable by whoever is sitting
    in front of it.

    The id is what makes this worth a round trip. Without one the agent can
    only text-search and hope, and a store that answers every query with its
    nearest guess will hand back a different product with almost the same
    name — a confident, wrong card. ``product_id`` removes the guess.

    Takes no arguments on purpose. "Which page?" has exactly one answer at any
    moment, and a parameter would invite the model to ask about a page the
    shopper is not on.
    """
    return FlowsFunctionSchema(
        name=GET_CURRENT_PAGE_PRODUCT_TOOL_NAME,
        description=(
            "Find out which item the shopper is looking at right now. Call "
            "this whenever they refer to something by where it is rather "
            "than by name — 'this', 'this one', 'the one I'm looking at', "
            "'tell me about this product' — or ask for details about an item "
            "they have not named. "
            "Returns 'product_id' (the catalogue's own id for the item on "
            "the page, when it could be identified), plus 'product_title', "
            "'url' and 'title'. "
            "WITH a product_id: that product's card is ALREADY on screen, "
            "drawn from its exact id, and its catalogue read is in your "
            "context. Do NOT search the catalogue and do NOT render a card "
            "of your own — both are refused. Write the line that belongs "
            "with the card (what the page does not already say; sizes are in "
            "the read's 'options') and ask what they need. "
            "WITHOUT a product_id: the page could not be identified. Say so "
            "and ask which item they mean. Do NOT search by the page title "
            "and show what comes back — an unverified match is a DIFFERENT "
            "product more often than it looks. "
            "A page that is not a product page — a home page, a collection, "
            "an article — simply returns no product_id; treat that as the "
            "answer, not as a failure to report."
        ),
        properties={},
        required=[],
        handler=_never_executes,
    )


#: name -> builder, for the tools this engine can put on the wire.
CLIENT_TOOL_BUILDERS = {
    GET_CURRENT_PAGE_PRODUCT_TOOL_NAME: build_get_current_page_product_schema,
}
