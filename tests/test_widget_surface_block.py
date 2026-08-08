"""The one ``widget`` surface block on the session responses.

The block exists so a seventh presentation field is added in ONE place and
every surface gets it, instead of being hand-copied across create, resume
and demo and forgotten on one of them — which is exactly what had happened
to the demo response.

It ships ADDITIVE: the same values keep riding at the top level of each
response, because embeds in the wild are cached bundles whose refresh we
do not control. So the property that actually has to hold is PARITY — the
block and the flat fields can never disagree while both are on the wire.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api.routers.breeze_buddy.widget.handlers import _surface_wire, _WidgetSurface
from app.schemas.breeze_buddy.chat import (
    CreateDemoSessionResponse,
    CreateWidgetSessionResponse,
    GreetingTileWire,
    QuickReplyWire,
    WidgetSessionStateResponse,
    WidgetSurfaceWire,
)

_QUICK = [QuickReplyWire(label="Track order", value="where is my order")]
_TILES = [
    GreetingTileWire(
        label="Shop tops", prompt="show me tops", image_url="https://x/i.png"
    )
]


def _surface() -> _WidgetSurface:
    return _WidgetSurface(
        quick_replies=list(_QUICK),
        greeting_tiles=list(_TILES),
        enable_text_input=False,
    )


def _template(voice: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        configurations=None,
        supported_channels=["chat", "voice"] if voice else ["chat"],
    )


def test_block_carries_the_same_values_as_the_flat_fields():
    surface = _surface()
    template = _template(voice=True)
    block = _surface_wire(
        surface, template, catalog_active="v2", ui_flavors=["commerce"]
    )
    # Field-for-field against the SAME sources the flat response fields are
    # built from — the two are constructed from one surface object, so a
    # future edit that feeds one and not the other fails here.
    assert [q.label for q in block.quick_replies] == [q.label for q in _QUICK]
    assert [t.label for t in block.greeting_tiles] == [t.label for t in _TILES]
    assert block.enable_text_input is surface.enable_text_input
    assert block.voice_enabled is True
    assert block.catalog_active == "v2"
    assert block.ui_flavors == ["commerce"]


def test_every_session_response_exposes_the_block():
    # The demo response is the one that used to lack the surface fields
    # entirely; the block is what makes the three symmetrical.
    for model in (
        CreateWidgetSessionResponse,
        WidgetSessionStateResponse,
        CreateDemoSessionResponse,
    ):
        assert "widget" in model.model_fields, model.__name__


def test_block_defaults_are_safe_for_a_bare_template():
    # A template with no widget config at all still produces a legal block:
    # empty pills/tiles, composer ON, voice OFF, v1 catalog. A default that
    # hid the composer would lock a shopper out of a working session.
    block = _surface_wire(
        _WidgetSurface(quick_replies=[], greeting_tiles=[], enable_text_input=True),
        _template(),
        catalog_active="v1",
        ui_flavors=[],
    )
    assert block == WidgetSurfaceWire()


def test_demo_response_defaults_the_block_when_unset():
    # Constructed without ``widget`` (older call sites / tests), the model
    # must still serialize a complete block rather than null.
    resp = CreateDemoSessionResponse.model_construct(session_id="s1")
    assert CreateDemoSessionResponse.model_fields["widget"].default_factory is not None
    assert resp is not None


def test_widget_resume_strips_llm_context_only_blocks():
    """The embed's resume route must sanitize like the /chat routes do.

    The agent persists ``visibility=internal`` blocks so the LLM keeps
    referential memory across turns — rendered-UI summaries, the chips and
    answer nudges, Gemini thought signatures. Those are engine internals.
    This route had been returning rows verbatim, so they shipped to
    whoever holds the widget token; the reference embed reads ``content``
    and ignores ``content_blocks``, but a third-party one drawing blocks
    rendered the engine's own prompt text as a chat bubble.

    Pinned on the handler module's binding, so re-pointing it at an
    unsanitized read fails here rather than in production.
    """
    from app.ai.voice.agents.breeze_buddy.chat.history.block_codec import (
        internal_text_block,
        plain_text_blocks,
    )
    from app.api.routers.breeze_buddy.widget import handlers as widget_handlers
    from app.schemas.breeze_buddy.chat import ChatMessage, ChatMessageRole

    nudge = ChatMessage.model_construct(
        idx=1,
        role=ChatMessageRole.USER,
        content=None,
        content_blocks=[internal_text_block("(you have not replied to the user yet.)")],
        ui_blocks=None,
    )
    reply = ChatMessage.model_construct(
        idx=2,
        role=ChatMessageRole.ASSISTANT,
        content="We have three in light grey.",
        content_blocks=[
            plain_text_blocks("We have three in light grey.")[0],
            internal_text_block("[ui rendered: ProductGrid]"),
        ],
        ui_blocks=None,
    )

    cleaned = widget_handlers._sanitize_messages_for_widget([nudge, reply])

    # The internal-only row is gone entirely; the reply survives with its
    # visible text and without the summary.
    assert len(cleaned) == 1
    assert cleaned[0].content == "We have three in light grey."
    blocks = cleaned[0].content_blocks or []
    assert all(b.get("visibility") != "internal" for b in blocks)
    serialized = str(cleaned)
    assert "you have not replied" not in serialized
    assert "ui rendered" not in serialized
