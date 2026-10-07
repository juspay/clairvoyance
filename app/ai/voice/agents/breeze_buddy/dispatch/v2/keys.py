"""Redis key names for the v2 event dialler (docs/dispatch-v2/design-card.md §1)."""

# When match can next issue on each number (ZSET number -> ms): the 1 s sweep matches
# only the numbers due now (design card rule 55). match rewrites its number's entry;
# enqueue, the reaper's re-queue, a route write, a raised max and the kill switch coming
# back on list a number as due.
DUE_KEY = "bb:due"
# Every ticket match issues, in issue order, as "number|lead|ticket id|template|issued_ms":
# one list for all numbers, popped by each pod's acceptor (design card §4).
TICKETS_KEY = "bb:tickets"
# Lines reserved for a workflow call whose lead row does not exist yet, as a ticket's five
# fields plus the run id; popped by the grant worker (grants.py), which makes the row and
# then publishes the ticket.
GRANTS_KEY = "bb:grants"
ENABLED_MIRROR_KEY = "bb:dispatch:enabled"
EPOCH_KEY = "bb:epoch"
# Numbers in a v2-accounted mode (v2_pending, v2, draining), and numbers whose hand-back
# to today's dialler is not finished yet; maintained by switch.py.
V2_ACTIVE_KEY = "bb:v2:active"
# Leader lock of the 1 s sweep (sweep.py); watched by monitor.py.
SWEEP_LEADER_KEY = "bb:v2:sweep:leader"

# bb:route:{T} expires this long after its last write (card §1 lifetimes, Fable M10): a
# missing route is re-resolved on use (ensure_route), so evicting one only delays leads.
# bb:num:{N} never expires: prod Redis is volatile-lru, so a key with a TTL can be evicted
# when memory is tight, and losing a number's mode / seq would switch it off v2 with no
# hand-back (today's dialler on a stale DB channels count: over-dial).
ROUTE_TTL_S = 2 * 24 * 3600


def route_key(template_id: str) -> str:
    return f"bb:route:{template_id}"


def num_key(number_id: str) -> str:
    return f"bb:num:{number_id}"


def numtpl_key(number_id: str) -> str:
    return f"bb:numtpl:{number_id}"


def room_key(template_id: str) -> str:
    return f"bb:q:{template_id}"


def qp_key(template_id: str) -> str:
    # not bb:q:*: the orphan prune reads the rest of such a name as a template id
    return f"bb:qp:{template_id}"


def qi_key(template_id: str) -> str:
    # member -> run id, for a workflow call that waits with no lead row yet
    return f"bb:qi:{template_id}"


def qa_key(template_id: str) -> str:
    # member -> how many times its grant freed the line (REGRANT)
    return f"bb:qa:{template_id}"


def busy_key(number_id: str) -> str:
    return f"bb:busy:{number_id}"


def inflight_key(number_id: str) -> str:
    return f"bb:inflight:{number_id}"
