"""The fields a shop's assistant is built from, as a shopkeeper would name them.

Research fills what it can read off the site; the merchant edits the same
list afterwards, so anything research got wrong can be put right. Every field
is a question a shopkeeper can answer.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.verticals.fields import (
    FieldProfile,
    FieldSection,
    FieldSpec,
)

STORE_FIELDS = FieldProfile(
    sections=(
        FieldSection(
            key="identity",
            title="Your store",
            brief="Who you are, in the words the assistant will use.",
            fields=(
                FieldSpec(
                    "brand_line",
                    "Your store's name",
                    "As shoppers know it.",
                    kind="line",
                ),
                FieldSpec(
                    "what_we_sell",
                    "What you sell",
                    "A short paragraph in your own words.",
                    kind="text",
                    from_research=True,
                ),
                FieldSpec(
                    "vocabulary",
                    "How it should sound",
                    "The words you use for your goods, and your tone.",
                    kind="text",
                    from_research=True,
                ),
                FieldSpec(
                    "assistant_name",
                    "What it calls itself",
                    "Shoppers see this when it introduces itself.",
                    kind="line",
                ),
                FieldSpec(
                    "tagline",
                    "Tagline",
                    "Your slogan, word for word.",
                    kind="line",
                    from_research=True,
                ),
                FieldSpec(
                    "compliance",
                    "Anything shoppers must be told",
                    "Legal or safety notes the assistant must always give.",
                    kind="text",
                    from_research=True,
                ),
            ),
        ),
        FieldSection(
            key="lead",
            title="What it leads with",
            brief="The three lists it reaches for before anything else.",
            fields=(
                FieldSpec(
                    "hero_items",
                    "Products to put forward",
                    "Names only: prices and stock always come from your live "
                    "catalogue.",
                    kind="list",
                    from_research=True,
                ),
                FieldSpec(
                    "offer_items",
                    "Offers running now",
                    "Clear the list when a sale ends.",
                    kind="list",
                    from_research=True,
                ),
                FieldSpec(
                    "trust_items",
                    "Why shoppers trust you",
                    "Guarantees, authenticity, how long you have been at it.",
                    kind="list",
                    from_research=True,
                ),
            ),
        ),
        FieldSection(
            key="policies",
            title="Your policies",
            brief="As your site states them. The assistant quotes these and "
            "never makes one up.",
            fields=(
                FieldSpec(
                    "returns",
                    "Returns and exchanges",
                    kind="list",
                    from_research=True,
                ),
                FieldSpec(
                    "delivery",
                    "Delivery",
                    "Times, costs and where you ship.",
                    kind="list",
                    from_research=True,
                ),
                FieldSpec(
                    "faq",
                    "Common questions",
                    "The questions shoppers ask before buying, one per line, as "
                    "'Q: ... A: ...'. Every shop has one that decides the sale: "
                    "size for clothes, fit for shoes, compatibility for parts.",
                    kind="list",
                    from_research=True,
                ),
            ),
        ),
        FieldSection(
            key="escalation",
            title="When it needs a human",
            brief="Where it sends a shopper it cannot help.",
            fields=(
                FieldSpec(
                    "whatsapp",
                    "WhatsApp number",
                    "With country code. Becomes a WhatsApp button in the chat.",
                    kind="phone",
                    from_research=True,
                ),
                FieldSpec(
                    "email",
                    "Support email",
                    kind="email",
                    from_research=True,
                ),
                FieldSpec(
                    "help_links",
                    "Other places to send shoppers",
                    "Returns portal, contact form, order tracking, with the "
                    "full https address.",
                    kind="list",
                ),
            ),
        ),
        FieldSection(
            key="surface",
            title="First screen",
            brief="What a shopper sees before typing.",
            fields=(
                FieldSpec(
                    "initial_greeting",
                    "Greeting",
                    "Two short lines: a question, then an offer.",
                    kind="text",
                ),
                FieldSpec(
                    "quick_replies",
                    "Quick replies",
                    "Three or four things a shopper would tap.",
                    kind="list",
                ),
                FieldSpec(
                    "greeting_tiles",
                    "Greeting tiles",
                    "One per line: label | what it asks | image address from "
                    "your own store.",
                    kind="list",
                ),
            ),
        ),
    ),
)

__all__ = ["STORE_FIELDS"]
