"""The brand lane: what Firecrawl's branding answer becomes. No network."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from app.ai.voice.agents.breeze_buddy.assist.engine.research.providers import (
    firecrawl,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding import branding


@pytest.mark.parametrize(
    "payload, logo",
    [
        (
            {"images": {"logo": "https://store.example/a.png"}},
            "https://store.example/a.png",
        ),
        ({"logo": "https://store.example/b.png"}, "https://store.example/b.png"),
        ({"images": {"favicon": "https://store.example/f.ico"}}, None),
    ],
)
def test_firecrawl_logo_is_read_from_its_logo_fields(
    payload: Dict[str, Any], logo: Optional[str]
) -> None:
    assert firecrawl.parse_branding(payload).logo_url == logo


async def test_without_firecrawl_the_look_is_empty_not_an_error(monkeypatch) -> None:
    monkeypatch.setattr(firecrawl, "FIRECRAWL_API_KEY", "")
    look = await branding.read_branding("https://store.example/")
    assert look == firecrawl.Branding()


async def test_firecrawl_is_told_how_long_we_wait(monkeypatch) -> None:
    sent: Dict[str, Any] = {}

    class _Response:
        status = 500

        async def __aenter__(self) -> "_Response":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        def post(self, url: str, *, headers: Any, json: Dict[str, Any]) -> _Response:
            sent.update(json)
            return _Response()

    monkeypatch.setattr(firecrawl, "FIRECRAWL_API_KEY", "key")
    monkeypatch.setattr(firecrawl, "create_aiohttp_session", lambda **_: _Session())
    with pytest.raises(firecrawl.WebsiteScrapingUpstreamError):
        await firecrawl.brand_look("https://store.example/")
    assert sent["timeout"] == 60000


def test_the_site_icon_is_read_apart_from_the_logo() -> None:
    look = firecrawl.parse_branding(
        {
            "images": {
                "logo": "https://s.example/word.png",
                "favicon": "https://s.example/i.png",
            }
        }
    )
    assert (look.logo_url, look.icon_url) == (
        "https://s.example/word.png",
        "https://s.example/i.png",
    )


@pytest.mark.parametrize(
    "data",
    [{"branding": None}, {"branding": {"colors": ["#c22126"]}}, None],
)
async def test_an_odd_branding_answer_is_a_provider_error(monkeypatch, data) -> None:
    class _Response:
        status = 200

        async def __aenter__(self) -> "_Response":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def json(self, **_: Any) -> Dict[str, Any]:
            return {"success": True, "data": data}

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        def post(self, *_: Any, **__: Any) -> _Response:
            return _Response()

    monkeypatch.setattr(firecrawl, "FIRECRAWL_API_KEY", "key")
    monkeypatch.setattr(firecrawl, "create_aiohttp_session", lambda **_: _Session())
    with pytest.raises(firecrawl.WebsiteScrapingUpstreamError):
        await firecrawl.brand_look("https://store.example/")


# Firecrawl's answers for real stores (6 Oct 2026), cut to what is read.
@pytest.mark.parametrize(
    "branding, primary",
    [
        (  # thebeyondbound.com: its own button
            {
                "colors": {"primary": "#C01352", "background": "#FFFFFF"},
                "components": {"buttonPrimary": {"background": "#BA1953"}},
            },
            "#ba1953",
        ),
        (  # kosha.co.in: the render's primary is a grey; the button is theirs
            {
                "colors": {"primary": "#868E96", "secondary": "#28A745"},
                "components": {"buttonPrimary": {"background": "#DFD8D2"}},
            },
            "#dfd8d2",
        ),
        (  # sensesindia.in: no button, a dark site whose black is the brand
            {
                "colorScheme": "dark",
                "colors": {"primary": "#FDECEC", "background": "#1C1C1C"},
                "components": {"input": {}},
            },
            "#1c1c1c",
        ),
        (  # no button, a light site: the render's own primary
            {
                "colorScheme": "light",
                "colors": {"primary": "#FB641B", "background": "#FFFFFF"},
                "components": {"buttonPrimary": {"background": "transparent"}},
            },
            "#fb641b",
        ),
        ({"colors": {}}, None),
    ],
)
def test_the_primary_is_the_stores_button_then_a_dark_background(
    branding: Dict[str, Any], primary: Optional[str]
) -> None:
    assert firecrawl.parse_branding(branding).primary_color == primary


@pytest.mark.parametrize("bad", ["#zzz", "#12345g", "####", "#-1-", "rgb(0,0,0)"])
def test_only_a_real_hex_code_becomes_the_colour(bad: str) -> None:
    look = firecrawl.parse_branding(
        {
            "colors": {"primary": "#FB641B"},
            "components": {"buttonPrimary": {"background": bad}},
        }
    )
    assert look.primary_color == "#fb641b"


async def test_a_store_address_too_long_to_send_is_no_look(monkeypatch) -> None:
    monkeypatch.setattr(firecrawl, "FIRECRAWL_API_KEY", "key")
    look = await branding.read_branding("https://s.example/" + "%C3%A9" * 400)
    assert look == firecrawl.Branding()
