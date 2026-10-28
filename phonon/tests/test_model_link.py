import asyncio

from stt.config import Settings
from stt.frontend.model_link import ModelLink


def test_results_after_the_loop_closed_are_dropped_not_crashing():
    # Review 2 #5: a batch arriving while uvicorn shuts down must not kill the
    # receiver thread (which would turn every graceful stop into exit 1).
    link = ModelLink(conn=None, settings=Settings(), label="fe1-1")  # type: ignore[arg-type]
    loop = asyncio.new_event_loop()
    link._loop = loop
    loop.close()
    assert link._post(print, "late result") is False
