"""A store's brand look, read on request for the console to preview."""

from __future__ import annotations

import time

from app.ai.voice.agents.breeze_buddy.assist.engine.classify import classify_profile
from app.ai.voice.agents.breeze_buddy.assist.engine.models import BrandLook
from app.ai.voice.agents.breeze_buddy.assist.engine.probe import probe_site
from app.ai.voice.agents.breeze_buddy.assist.engine.research import brand
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry


async def read_look(url: str, *, budget_seconds: float) -> BrandLook:
    """The site's brand colours and logo, read now and saved nowhere.

    The platform is worked out from the home page, so its stock colours are
    never offered as the brand's. Done within ``budget_seconds``: a slow render
    drops that one source, not the whole look. Raises what the fetch raises
    (``UnsafeUrlError``, ``FetchFailedError``, ``EgressNotGuardedError``).
    """
    deadline = time.monotonic() + budget_seconds
    profile = await probe_site(url)
    adapter = registry.resolve(classify_profile(profile).adapter_id)
    return await brand.resolve(
        profile.final_url or url,
        profile,
        stock_colors=adapter.stock_colors(),
        deadline=deadline,
    )


__all__ = ["read_look"]
