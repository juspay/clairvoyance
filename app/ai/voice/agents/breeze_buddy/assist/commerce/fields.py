"""The fields a shop's assistant is built from, as a shopkeeper would name them.

Research fills what it can read off the site; the merchant edits the same
list afterwards. Every shown field is a question a shopkeeper can answer.
``hidden`` fields still reach the prompt with whatever research found (the
tagline, the compliance line, the store's policies); they are just not put to
the merchant as questions.

Taken from #1209 (commerce/slots.py), keeping only the fields the assistant is
built from today.
"""

from __future__ import annotations

from typing import Tuple

from app.ai.voice.agents.breeze_buddy.assist.engine.fields import (
    FieldProfile,
    FieldSection,
    FieldSpec,
)


def _question(
    index: int, heading_example: str, body_example: str
) -> Tuple[FieldSpec, FieldSpec]:
    """One deciding-question pair: the question, and how to answer it."""
    suffix = "" if index == 1 else f"_{index}"
    return (
        FieldSpec(
            f"question{suffix}",
            "The question",
            (
                "Every shop has one: size for clothes, fit for shoes, "
                "dimensions for furniture, compatibility for parts."
                if index == 1
                else ""
            ),
            kind="line",
            example=heading_example,
            group="question",
            index=index,
        ),
        FieldSpec(
            f"question_answer{suffix}",
            "What it needs to know to answer it",
            (
                "Your size chart in words, how your fit runs, the measurements "
                "that matter, so the assistant settles it in the chat."
                if index == 1
                else ""
            ),
            kind="text",
            example=body_example,
            group="question",
            index=index,
        ),
    )


STORE_FIELDS = FieldProfile(
    name="store",
    sections=(
        FieldSection(
            key="identity",
            title="Your store",
            brief="Who you are, in the words the assistant will use.",
            fields=(
                FieldSpec(
                    "brand_line",
                    "Your store in one line",
                    "Who you are and what stands behind you.",
                    kind="line",
                    example="Kosha: merino base layers for Indian winters, "
                    "sold direct since 2016.",
                    from_notes=("brand_line",),
                ),
                FieldSpec(
                    "what_we_sell",
                    "What you sell",
                    "A short paragraph in your own words.",
                    kind="text",
                    example="Merino base layers, thermals and socks in three "
                    "weights, knitted in-house.",
                    from_notes=("what_we_sell",),
                ),
                FieldSpec(
                    "vocabulary",
                    "How it should sound",
                    "The words you use for your goods, and your tone.",
                    kind="text",
                    example="We say 'pieces', never 'items'. Warm and plain.",
                    from_notes=("vocabulary",),
                ),
                FieldSpec(
                    "assistant_name",
                    "What it calls itself",
                    "Shoppers see this when it introduces itself.",
                    kind="line",
                    example="Kosha Assist",
                ),
                FieldSpec(
                    "tagline",
                    "Tagline",
                    hidden=True,
                    from_notes=("tagline",),
                ),
                FieldSpec(
                    "compliance",
                    "Compliance note",
                    hidden=True,
                    from_notes=("compliance",),
                ),
                FieldSpec(
                    "policies",
                    "Store policies",
                    "Returns, delivery and common questions, as the site "
                    "states them.",
                    many=True,
                    hidden=True,
                    from_notes=("returns", "delivery", "faq"),
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
                    many=True,
                    kind="list",
                    example="Ultralight Merino Crew",
                    from_notes=("hero_items",),
                ),
                FieldSpec(
                    "offer_items",
                    "Offers running now",
                    "Clear the list when a sale ends.",
                    many=True,
                    kind="list",
                    example="Winter sale: 20% off all thermals until 31 Jan",
                    from_notes=("offer_items",),
                ),
                FieldSpec(
                    "trust_items",
                    "Why shoppers trust you",
                    "Guarantees, authenticity, how long you have been at it.",
                    many=True,
                    kind="list",
                    example="Free returns within 15 days, no questions",
                    from_notes=("trust_items",),
                ),
            ),
        ),
        FieldSection(
            key="question",
            title="The question that decides the sale",
            brief="Answer it in the chat and they buy. Up to three.",
            fields=(
                *_question(
                    1,
                    "Which weight and size should I get?",
                    "Sizes run true to chest; between two sizes, go up.",
                ),
                *_question(
                    2,
                    "Will it survive a wash?",
                    "Machine wash cold, dry flat.",
                ),
                *_question(
                    3,
                    "Is this warm enough for where I am going?",
                    "160gsm to about 5°C with a shell, 260gsm below freezing.",
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
                    example="+91 98765 43210",
                    from_notes=("whatsapp",),
                ),
                FieldSpec(
                    "email",
                    "Support email",
                    kind="email",
                    example="care@kosha.example",
                    from_notes=("email",),
                ),
                FieldSpec(
                    "escalation_extra",
                    "Other places to send shoppers",
                    "Returns portal, contact form, order tracking, with the "
                    "full https address.",
                    many=True,
                    kind="list",
                    example="Track your order: https://kosha.example/track",
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
                    many=True,
                    kind="list",
                ),
                FieldSpec(
                    "greeting_tiles",
                    "Greeting tiles",
                    "One per line: label | what it asks | image address from "
                    "your own store.",
                    many=True,
                    kind="list",
                ),
            ),
        ),
    ),
)

__all__ = ["STORE_FIELDS"]
