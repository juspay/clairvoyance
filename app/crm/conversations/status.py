"""Every word conversations BRANCHES on, one section per table — the one
home for each (SQL binds them as $n, never spells them; the vocabulary test
walks the builders).

Who holds a thread is never stored as a word: it is DERIVED (state.py) from
the thread row and its open handoff, so these are the words the rows carry
and the views the Inbox asks for.
"""

# --- crm_conversation_message.kind (a closed triple, CHECKed) ---------------
KIND_INBOUND = "inbound"
KIND_OUTBOUND = "outbound"
KIND_NOTE = "note"

# --- crm_conversation_message.author_kind (vocabulary, no CHECK) ------------
AUTHOR_CUSTOMER = "customer"
#: Buddy (an Assist agent) — its replies.
AUTHOR_ASSIST = "assist"
AUTHOR_TEAMMATE = "teammate"
#: A workflow's template, on a number it shares with Buddy.
AUTHOR_WORKFLOW = "workflow"

# --- crm_conversation.channel (the channels registry's words) ---------------
CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_WIDGET = "widget"

# --- crm_handoff.outcome (vocabulary, no CHECK) -----------------------------
#: A teammate (or Buddy, ending the chat) resolved the thread.
OUTCOME_RESOLVED = "resolved"
#: The teammate handed the thread back to Buddy.
OUTCOME_HANDED_BACK = "handed_back"
#: Nobody claimed it inside the claim SLA; Buddy resumed.
OUTCOME_SLA_LAPSED = "sla_lapsed"
#: The reply window ran out while it was open.
OUTCOME_EXPIRED = "expired"
#: Buddy moved to another number; the thread on the old one was resolved.
OUTCOME_NUMBER_CHANGED = "number_changed"

# --- who holds a thread (DERIVED in state.py, never stored) -----------------
HELD_BY_TEAMMATE = "teammate"
#: An open handoff nobody has claimed: Buddy is silent, "Needs attention".
HELD_WAITING = "waiting"
HELD_BY_BUDDY = "buddy"
#: Nobody: no assignee, no handoff, no agent — Inbox "Unassigned".
HELD_BY_NOBODY = "unattended"
HELD_RESOLVED = "resolved"

# --- the Inbox's views (GET /conversations?view=) ---------------------------
VIEW_NEEDS_ATTENTION = "needs_attention"
VIEW_MINE = "mine"
VIEW_BUDDY = "buddy"
VIEW_UNASSIGNED = "unassigned"
VIEW_RESOLVED = "resolved"
VIEW_ALL = "all"
VIEWS = (
    VIEW_NEEDS_ATTENTION,
    VIEW_MINE,
    VIEW_BUDDY,
    VIEW_UNASSIGNED,
    VIEW_RESOLVED,
    VIEW_ALL,
)

# --- the live stream's wake-up kinds (SSE "thread" events) ------------------
WAKE_MESSAGE = "message"
WAKE_STATE = "state"
WAKE_HANDOFF = "handoff"
WAKE_READ = "read"
#: One of our sends on the thread moved (sent · delivered · read · failed):
#: the Inbox re-reads its ticks.
WAKE_RECEIPT = "receipt"
