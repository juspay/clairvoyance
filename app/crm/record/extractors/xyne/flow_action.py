"""A Xyne flow action — a person pressed submit on a form a Xyne app posted.

The bay files Xyne's callback verbatim, submits only
(connectivity/providers/xyne/inbound.py): ``{actionId, type, values,
context: {flowJSON, messageId, conversationId, userId}}``. What a plan can
use, and why each reading is safe:

  userId         WHO answered — identity ``xyne_user_id`` (a row kind). Xyne's
                 server fills it from the login; the person cannot change it.
  reply          ``form_submitted``, the word every form source answers with
                 (extractors/forms.py): the arrow out of a listening square.
  button         which button was pressed (its ``actionId``) — a form may
                 offer more than one. As the person's browser sent it: Xyne
                 does not check it against the form, so it is the person's
                 claim, like their answers. (Not ``action_id``: ``action_``
                 is the walker's bookkeeping prefix, and a run would drop it.)
  flow_response  every answer as ``name: value`` lines: one blank, any form,
                 no per-form parsing.
  replied_to     ``context.messageId`` — which of OUR sends this answers; the
                 reply join (outreach/reply_attribution.py) finds the run, and
                 only ever among the runs of the person ``userId`` resolved to.

Never read: ``context.flowJSON`` — the browser's copy of the form, which the
person can edit, ``data`` included — and ``conversationId``. Both stay on the
event row, where the letter is kept as sent.
"""

from typing import Any, Dict, List, Optional

from app.crm.record.extractors.forms import FORM_SUBMITTED, render_answers
from app.crm.record.schemas import CatalogField

#: Xyne's own event name (X-Xyne-Event). Spelled here because record imports
#: no other module (boundary rule 12); tests/crm/test_xyne_answers.py pins it
#: equal to connectivity/topics.py's TOPIC_FLOW_ACTION.
TOPIC = "flow_action"

#: The form callback's type for a submit (an inputChange is never filed).
SUBMIT = "submit"


def reply(payload: Dict[str, Any]) -> Optional[str]:
    """``form_submitted`` for a submit — recognised by its envelope, never by
    what was filled in: a form with no fields is still an answer."""
    return FORM_SUBMITTED if payload.get("type") == SUBMIT else None


def button(payload: Dict[str, Any]) -> Optional[str]:
    """Which button: the ``actionId`` the person's submit carried."""
    value = payload.get("actionId")
    return value if isinstance(value, str) and value else None


def flow_response(payload: Dict[str, Any]) -> Optional[str]:
    """What the person filled in, written out to be read by a person — the
    form's answers as ``name: value`` lines, in the order Xyne sent them."""
    values = payload.get("values")
    return render_answers(values) if isinstance(values, dict) else None


def replied_to(payload: Dict[str, Any]) -> Optional[str]:
    """The id of the message the form was posted in — the id Xyne returned
    when we sent it, so the manifest already knows whose send it was."""
    context = payload.get("context")
    message_id = context.get("messageId") if isinstance(context, dict) else None
    return message_id if isinstance(message_id, str) and message_id else None


def fields() -> List[CatalogField]:
    """The person (an identity, never a variable), the answer at two grains,
    the button, and the join."""
    return [
        CatalogField(
            path="payload.context.userId",
            type="text",
            label="Xyne user",
            identity="xyne_user_id",
        ),
        CatalogField(
            path="reply",
            type="text",
            label=f"Reply ('{FORM_SUBMITTED}')",
            variable=True,
            derived=True,
        ),
        CatalogField(
            path="button",
            type="text",
            label="Button pressed (actionId)",
            variable=True,
            derived=True,
        ),
        CatalogField(
            path="flow_response",
            type="text",
            label="Form answers",
            variable=True,
            derived=True,
        ),
        CatalogField(
            path="replied_to",
            type="text",
            label="Answered message id",
            keyable=True,
            derived=True,
        ),
    ]
