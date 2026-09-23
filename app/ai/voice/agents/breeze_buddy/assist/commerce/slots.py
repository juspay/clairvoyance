"""The fields a shop's assistant is built from — as a shopkeeper would name them.

Onboarding fills these by reading the site; the studio shows the same list
back for editing. One list under one set of keys, or the two screens describe
different agents and the merchant has to learn both.

**Every field here is a question a shopkeeper can answer.** That is the whole
selection rule, and it removed a third of this list. "Hero products —
preamble" and "section heading" are things a prompt author writes; "vocabulary
register" and "Compliance note" are things a consultant says. A merchant asked
those either leaves them alone or fills them in wrong, and a field nobody can
answer is worse than no field: it sits on the screen looking unfinished.

What went is not gone. ``hidden`` fields still reach the prompt and the widget
with whatever onboarding found — the storefront domain, the currency, the
basket URL, the tagline, the hero preamble, the compliance line, the trusted
link list. They are simply not put to the merchant as questions, because the
platform already knows them or research already answered them. The studio's
save path lays edits over the live values, so a field nobody was shown keeps
what it had rather than being blanked by its own absence.

Four fields the studio has are NOT here at all: ``merchant_id``,
``reseller_id``, ``template_name`` and ``model``. Those are decisions the
console already collects or the platform already knows, and no amount of
reading a website will produce them.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.engine.research.slots import (
    SlotField,
    SlotProfile,
    SlotSection,
)

PROFILE = SlotProfile(
    name="store",
    sections=[
        SlotSection(
            key="identity",
            title="Your store",
            heading="## Brand identity",
            brief="Who you are, in the words the assistant will use.",
            fields=[
                SlotField(
                    "brand_line",
                    "Your store in one line",
                    "Who you are and what stands behind you. Every reply is "
                    "written from this.",
                    kind="line",
                    example="Kosha — merino base layers made for Indian winters, "
                    "sold direct since 2016.",
                ),
                SlotField(
                    "what_we_sell",
                    "What you sell",
                    "A short paragraph in your own words: the ranges, the "
                    "materials, what makes yours different from the next shop's.",
                    kind="text",
                    example="Merino wool base layers, thermals and socks in three "
                    "weights, for trekkers and for city winters. Everything is "
                    "knitted in-house and sold only on our own site.",
                ),
                SlotField(
                    "vocabulary",
                    "How it should sound",
                    "The words you use for your own goods, and the tone that goes "
                    "with them. The assistant copies this rather than inventing a "
                    "voice.",
                    kind="text",
                    example="We say 'pieces' and 'layers', never 'items'. Warm and "
                    "plain — helpful neighbour, not a salesperson.",
                ),
                SlotField(
                    "assistant_name",
                    "What it calls itself",
                    "Shoppers see this when it introduces itself.",
                    kind="line",
                    example="Kosha Assist",
                    researched=False,
                ),
                # ── Known, not asked ──────────────────────────────────────
                # Facts the platform or the research already settled. They
                # still reach the prompt; they are just not questions.
                SlotField(
                    "domain",
                    "Storefront domain",
                    "The shop's own domain.",
                    hidden=True,
                ),
                SlotField(
                    "currency",
                    "Currency",
                    "As the shop shows it, e.g. INR (₹).",
                    hidden=True,
                ),
                SlotField(
                    "cart_url",
                    "Basket page",
                    "Absolute https. Drives checkout everywhere.",
                    hidden=True,
                    researched=False,
                ),
                SlotField(
                    "tagline",
                    "Tagline",
                    "Their own words, quoted where possible.",
                    hidden=True,
                ),
                SlotField(
                    "compliance",
                    "compliance note",
                    "Vertical guardrail, or *(none — …)* when none applies.",
                    hidden=True,
                ),
            ],
        ),
        SlotSection(
            key="lead",
            title="What it leads with",
            # Rendered inside the brand block, which the vertical assembles
            # whole — there is no heading of its own to write.
            heading="",
            brief="The three lists it reaches for before anything else.",
            fields=[
                SlotField(
                    "hero_items",
                    "Products to put forward",
                    "Names only — prices and stock always come from your live "
                    "catalogue, never from this list.",
                    many=True,
                    kind="list",
                    item="product",
                    example="Ultralight Merino Crew",
                ),
                SlotField(
                    "offer_items",
                    "Offers running now",
                    "It keeps mentioning these until you take them off, so clear "
                    "the list when a sale ends.",
                    many=True,
                    kind="list",
                    item="offer",
                    example="Winter sale — 20% off all thermals until 31 Jan",
                ),
                SlotField(
                    "trust_items",
                    "Why shoppers trust you",
                    "Guarantees, authenticity, the numbers you publish, how long "
                    "you have been at it.",
                    many=True,
                    kind="list",
                    item="reason",
                    example="Free returns within 15 days, no questions",
                ),
                SlotField(
                    "hero_note",
                    "How to use the product list",
                    "One line telling the assistant how to use the list above.",
                    hidden=True,
                ),
            ],
        ),
        SlotSection(
            key="vertical",
            title="The question that decides the sale",
            # The heading itself is a merchant field, so the rendered one wins.
            heading="",
            brief="Answer this in the chat and they buy; send them away to find "
            "out and they don't. Up to three, if the shop has more than one.",
            fields=[
                SlotField(
                    "vertical_heading",
                    "The question",
                    "Every shop has one — size for clothes, fit for shoes, "
                    "dimensions for furniture, compatibility for parts.",
                    kind="line",
                    example="Which weight and size should I get?",
                    group="question",
                    index=1,
                ),
                SlotField(
                    "vertical_body",
                    "What it needs to know to answer it",
                    "Your size chart in words, how your fit runs, the measurements "
                    "that matter. Written here, the assistant settles it in the "
                    "conversation instead of linking to a chart.",
                    kind="text",
                    example="Sizes run true to chest measurement; between two "
                    "sizes, go up for the 260gsm and stay put for the 160gsm. Ask "
                    "chest in inches and typical layering before recommending.",
                    group="question",
                    index=1,
                ),
                SlotField(
                    "vertical_heading_2",
                    "The question",
                    "",
                    kind="line",
                    example="Will it survive a wash and a Himalayan week?",
                    group="question",
                    index=2,
                ),
                SlotField(
                    "vertical_body_2",
                    "What it needs to know to answer it",
                    "",
                    kind="text",
                    example="Machine wash cold on a gentle cycle, dry flat. Merino "
                    "resists odour, so it goes four or five days between washes — "
                    "say so, it is the reason people buy it for trekking.",
                    group="question",
                    index=2,
                ),
                SlotField(
                    "vertical_heading_3",
                    "The question",
                    "",
                    kind="line",
                    example="Is this warm enough for where I am going?",
                    group="question",
                    index=3,
                ),
                SlotField(
                    "vertical_body_3",
                    "What it needs to know to answer it",
                    "",
                    kind="text",
                    example="160gsm to about 5°C with a shell, 260gsm below "
                    "freezing. Ask the destination and the month before answering.",
                    group="question",
                    index=3,
                ),
            ],
        ),
        SlotSection(
            key="escalation",
            title="When it needs a human",
            heading="### Escalation channel",
            brief="Where it sends a shopper it cannot help. Without these it can "
            "only apologise.",
            fields=[
                SlotField(
                    "whatsapp",
                    "WhatsApp number",
                    "With country code. Becomes a WhatsApp button in the chat.",
                    kind="phone",
                    example="+91 98765 43210",
                ),
                SlotField(
                    "email",
                    "Support email",
                    "Offered when a question needs a person and WhatsApp is not "
                    "the way you work.",
                    kind="email",
                    example="care@kosha.example",
                ),
                SlotField(
                    "escalation_extra",
                    "Other places to send shoppers",
                    "Returns portal, contact form, order tracking. Paste the full "
                    "https address and the assistant can offer it as a button.",
                    many=True,
                    kind="list",
                    item="link",
                    example="Track your order — https://kosha.example/track",
                ),
                SlotField(
                    "trusted_extra",
                    "Trusted links",
                    "Addresses the agent may render as a button. The basket, "
                    "WhatsApp and anything linked above are added automatically.",
                    many=True,
                    hidden=True,
                ),
            ],
        ),
        SlotSection(
            key="surface",
            title="Widget surface",
            heading="",
            brief="The first screen. Edited under Appearance, not here.",
            fields=[
                SlotField(
                    "initial_greeting",
                    "initial greeting",
                    "v2 house style: two short lines — a question, then an offer. "
                    'Not "Hi, I\'m X, how can I help?".',
                ),
                SlotField(
                    "quick_replies",
                    "quick replies",
                    "Three or four things a real shopper would tap, one per line.",
                    many=True,
                ),
                SlotField(
                    "greeting_tiles",
                    "greeting tiles",
                    "One per line as `label | what it asks | image url`. Use real "
                    "images from your own store — never a guessed address.",
                    many=True,
                ),
            ],
        ),
    ],
)

__all__ = ["PROFILE"]
