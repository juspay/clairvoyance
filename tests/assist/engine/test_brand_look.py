"""The brand lane: the logo reader, the guards and the layering. No network."""

from __future__ import annotations

import io
import time
from typing import Any, Dict, List, Optional

import aiohttp
import pytest
from PIL import Image

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    BrandColor,
    BrandLook,
    SiteProfile,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research import brand
from app.ai.voice.agents.breeze_buddy.assist.engine.research.providers import (
    firecrawl,
)

RED = "#c22126"
GOLD = "#b69d6c"


def _profile(**overrides: Any) -> SiteProfile:
    body: Dict[str, Any] = {
        "url": "https://store.example/",
        "final_url": "https://store.example/",
        "status": 200,
    }
    body.update(overrides)
    return SiteProfile(**body)


def test_an_oversized_logo_is_refused_before_decoding(monkeypatch) -> None:
    # A flat 3000x3000 PNG is a few KB on the wire and 36 MB once decoded.
    buffer = io.BytesIO()
    Image.new("RGBA", (3000, 3000), (194, 33, 38, 255)).save(buffer, format="PNG")
    monkeypatch.setattr(
        Image.Image, "load", lambda self: pytest.fail("decoded an oversized logo")
    )
    assert brand.raster_colors(buffer.getvalue()) == []


def test_guards_set_aside_stock_background_and_grey_colours() -> None:
    look = BrandLook(
        colors=[
            BrandColor(role="primary", hex="#96bf48", source="page"),
            BrandColor(role="secondary", hex="#fdfdfd", source="page"),
            BrandColor(role="accent", hex="#555555", source="page"),
            BrandColor(role="link", hex=GOLD, source="page"),
        ]
    )
    guarded = brand.promote_primary(brand.apply_guards(look, stock_colors=("#96bf48",)))
    assert guarded.color("primary") == GOLD  # promoted from link
    assert len(guarded.warnings) == 3
    assert look.color("primary") == "#96bf48"  # the input is left as it was


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


@pytest.mark.parametrize("logo_colours, primary", [([RED], RED), ([], None)])
async def test_a_colour_set_aside_from_one_source_leaves_the_other(
    monkeypatch, logo_colours, primary
) -> None:
    async def render(url: str, *, timeout_seconds: float) -> Optional[BrandLook]:
        return BrandLook(
            colors=[BrandColor(role="primary", hex="#96bf48", source="render")]
        )

    async def logo(url: str) -> List[str]:
        return logo_colours

    monkeypatch.setattr(firecrawl, "brand_look", render)
    monkeypatch.setattr(brand, "logo_colors", logo)
    look = await brand.resolve(
        "https://store.example/",
        _profile(meta={"og:logo": "https://store.example/logo.png"}),
        stock_colors=("#96bf48",),
    )
    assert look.color("primary") == primary
    # Checked after the guards: a render whose only colour was set aside
    # established nothing.
    assert ("no brand colour could be established" in look.warnings) is (
        primary is None
    )


async def test_a_logo_whose_body_breaks_gives_no_colours(monkeypatch) -> None:
    async def broken(url: str, **_: Any):
        raise aiohttp.ClientPayloadError("bad gzip")

    monkeypatch.setattr(brand, "fetch_page", broken)
    assert await brand.logo_colors("https://store.example/logo.png") == []


@pytest.mark.parametrize(
    "meta, logo_url",
    [
        (
            {"og:logo": "https://store.example/logo.png"},
            "https://store.example/logo.png",
        ),
        # A banner is sampled for colour but never shown as the logo.
        ({"og:image": "https://store.example/banner.jpg"}, None),
    ],
    ids=["named-logo", "banner"],
)
async def test_without_firecrawl_the_logo_gives_the_colours(
    monkeypatch, meta, logo_url
) -> None:
    monkeypatch.setattr(firecrawl, "FIRECRAWL_API_KEY", "")
    sampled: List[str] = []

    async def logo(url: str) -> List[str]:
        sampled.append(url)
        return [RED]

    monkeypatch.setattr(brand, "logo_colors", logo)
    look = await brand.resolve("https://store.example/", _profile(meta=meta))

    assert sampled == [next(iter(meta.values()))]
    assert look.color("primary") == RED
    assert look.logo_url == logo_url
    assert "brand provider not configured — skipped" in look.warnings


async def test_no_time_left_skips_the_render_but_not_the_logo(monkeypatch) -> None:
    async def render(url: str, *, timeout_seconds: float) -> Optional[BrandLook]:
        raise AssertionError("the render should not be asked")

    async def logo(url: str) -> List[str]:
        return [RED]

    monkeypatch.setattr(firecrawl, "brand_look", render)
    monkeypatch.setattr(brand, "logo_colors", logo)
    look = await brand.resolve(
        "https://store.example/",
        _profile(meta={"og:logo": "https://store.example/logo.png"}),
        deadline=time.monotonic() + 1,
    )
    assert look.color("primary") == RED
    assert "brand provider skipped — no time left" in look.warnings


def test_a_colour_too_pale_to_carry_text_is_set_aside() -> None:
    look = BrandLook(colors=[BrandColor(role="accent", hex="#fac7c7", source="page")])
    guarded = brand.apply_guards(look)
    assert guarded.colors == []
    assert guarded.warnings == ["accent #fac7c7 is too pale to carry text — ignored"]


def test_a_black_and_white_store_gets_its_black() -> None:
    page = BrandLook(
        colors=[
            BrandColor(role="accent", hex="#fac7c7", source="page"),
            BrandColor(role="background", hex="#1c1c1c", source="page"),
        ]
    )
    guarded = brand.promote_primary(brand.apply_guards(page))
    assert guarded.color("primary") is None
    look = brand.monochrome_primary(guarded, [page])
    assert look.color("primary") == "#1c1c1c"


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
    profile = _profile(
        link_rels=[
            {"rel": "icon", "href": "/favicon.png"},
            {"rel": "apple-touch-icon", "href": "/touch.png"},
        ]
    )
    assert brand.site_icons(profile) == [
        "https://store.example/touch.png",
        "https://store.example/favicon.png",
    ]
