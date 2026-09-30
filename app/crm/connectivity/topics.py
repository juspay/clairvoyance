"""The spine topics this module publishes and consumes — one file, one name
each.

These are canon T13 vocabulary, not any provider's: `template.status` is what
a letter about a template's approval is CALLED on the event spine, whatever
sent it. Meta's own word for the same thing is
`message_template_status_update`, and that mapping belongs in the bay that
receives it.

They live here rather than in providers/whatsapp/templates.py for a boundary
reason, not a tidiness one: the consumer that reads these letters is generic
(it dispatches through CONNECTORS), and rule 11 forbids it from importing a
provider package. A shared constant in a provider file is a boundary
violation waiting for its second caller. Same shape, and the same reason, as
reasons.py.
"""

#: A provider decided a template's fate — approved, rejected, paused, deleted.
TOPIC_TEMPLATE_STATUS = "template.status"

#: A provider re-categorised a template. The money one: the category is what
#: the merchant is billed at, and providers change it on their own.
TOPIC_TEMPLATE_CATEGORY = "template.category"

#: A provider's quality read on a template — the early warning before it
#: pauses one itself.
TOPIC_TEMPLATE_QUALITY = "template.quality"

#: Every template topic, for a consumer registering itself against all three.
TEMPLATE_TOPICS = (
    TOPIC_TEMPLATE_STATUS,
    TOPIC_TEMPLATE_CATEGORY,
    TOPIC_TEMPLATE_QUALITY,
)

#: What became of a message WE sent — one letter per transition, whatever
#: channel carried it (the channel rides in the event's source and payload).
TOPIC_STATUS = "message.status"

#: A customer wrote back to us.
TOPIC_INBOUND = "message.inbound"

#: We wrote a manifest row — a template queued for the dispatcher, or a
#: free-form reply sent inside the customer-service window. Filed by THIS
#: module at insert time (ADR 0014 amendment), so the letter carries what
#: the manifest deliberately never stores: the words of a free-form reply
#: (canon T16 keeps no rendered text). Its source is the CHANNEL, the same
#: word the provider's own letters about that channel carry.
TOPIC_QUEUED = "message.queued"

#: Buddy moved to another binding: the merchant chose a different binding
#: for Buddy to answer on, and its settings moved with it. Filed by THIS
#: module after the move commits (settings.py); the conversations module
#: resolves the old binding's open threads when it hears it. Source = the
#: channel.
TOPIC_BUDDY_MOVED = "binding.buddy_moved"

#: A provider said something about the connected account itself (a review
#: decision, a ban, a tier change) — the letters the health probe will read.
TOPIC_ACCOUNT = "account.update"
