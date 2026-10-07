"""The release script decides on the number's mode itself (Swaroop's #1313 review): a
number that is not v2 (for example one a hand-back just flipped to legacy) answers
"not v2" and is left untouched, so the caller runs today's release."""

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
from tests.breeze_buddy.dispatch.v2.conftest import seed_number


async def test_the_release_script_answers_not_v2_for_a_legacy_number(rr):
    await seed_number(rr, "N1", 1, {"T1": {}}, mode="legacy")
    await rr.sadd("bb:busy:N1", "lead:L1")
    assert await scripts.release("N1", "lead:L1") == [-1, 0]
    assert await rr.sismember("bb:busy:N1", "lead:L1")  # untouched


async def test_the_release_script_frees_a_draining_numbers_line(rr):
    await seed_number(rr, "N1", 1, {"T1": {}}, mode="draining")
    await rr.sadd("bb:busy:N1", "lead:L1")
    reply = await scripts.release("N1", "lead:L1")
    assert reply is not None and reply[0] == 1
    assert not await rr.sismember("bb:busy:N1", "lead:L1")
