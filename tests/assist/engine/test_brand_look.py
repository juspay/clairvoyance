"""The brand lane: Firecrawl, the logo reader, the guards and the layering.

No network: Firecrawl's session and the engine's fetcher are replaced.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional

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
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    FetchFailedError,
    FetchResult,
    UnsafeUrlError,
)

RED = "#c22126"
GOLD = "#b69d6c"


def _png(size=(64, 64), color=(194, 33, 38, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _profile(**overrides: Any) -> SiteProfile:
    body: Dict[str, Any] = {
        "url": "https://store.example/",
        "final_url": "https://store.example/",
        "status": 200,
    }
    body.update(overrides)
    return SiteProfile(**body)


# --- Firecrawl --------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    calls: List[Dict[str, Any]] = []

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def post(self, url, **kwargs):
        _FakeSession.calls.append({"url": url, **kwargs})
        return self._response


def _firecrawl_answers(monkeypatch, status=200, payload=None, key="fc-key") -> None:
    _FakeSession.calls = []
    monkeypatch.setattr(firecrawl, "FIRECRAWL_API_KEY", key)
    response = _FakeResponse(status, payload)
    monkeypatch.setattr(
        firecrawl, "create_aiohttp_session", lambda **_kw: _FakeSession(response)
    )


async def test_firecrawl_without_a_key_refuses_and_sends_nothing(monkeypatch):
    _firecrawl_answers(monkeypatch, key="")
    with pytest.raises(firecrawl.BrandProviderNotConfiguredError):
        await firecrawl.brand_look("https://store.example/")
    assert _FakeSession.calls == []


async def test_firecrawl_sends_the_key_and_parses_colours_and_logo(monkeypatch):
    _firecrawl_answers(
        monkeypatch,
        payload={
            "data": {
                "branding": {
                    "colors": {
                        "primary": "#C22126",
                        "background": "#ffffff",
                        "accent": "not-a-colour",
                    },
                    "images": {"logo": "https://cdn.example/logo.png"},
                }
            }
        },
    )
    look = await firecrawl.brand_look("store.example")

    call = _FakeSession.calls[0]
    assert call["url"] == firecrawl.ENDPOINT
    assert call["headers"]["Authorization"] == "Bearer fc-key"
    assert call["json"]["url"] == "https://store.example"
    assert look.color("primary") == RED
    assert look.color("background") == "#ffffff"
    assert look.color("accent") is None
    assert look.logo_url == "https://cdn.example/logo.png"


@pytest.mark.parametrize(
    "status,payload",
    [
        (500, {}),
        (200, ValueError("bad json")),
        (200, {"data": {}}),
        (200, ["not", "an", "object"]),
    ],
)
async def test_firecrawl_failures_are_unavailable_not_crashes(
    monkeypatch, status, payload
):
    _firecrawl_answers(monkeypatch, status=status, payload=payload)
    with pytest.raises(firecrawl.BrandLookUnavailable):
        await firecrawl.brand_look("https://store.example/")


async def test_firecrawl_refuses_a_private_url_before_calling_out(monkeypatch):
    _firecrawl_answers(monkeypatch, payload={})
    with pytest.raises(UnsafeUrlError):
        await firecrawl.brand_look("https://127.0.0.1/")
    assert _FakeSession.calls == []


def test_parse_branding_drops_http_logos_and_bad_hex():
    look = firecrawl.parse_branding(
        {
            "colors": {"primary": "#12345", "secondary": "#abc"},
            "images": {"logo": "http://cdn.example/logo.png"},
        }
    )
    assert [c.hex for c in look.colors] == ["#abc"]
    assert look.logo_url is None


# --- Logo decoding ----------------------------------------------------------


def test_raster_colors_reads_a_transparent_logo_mark():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    for x in range(16, 48):
        for y in range(16, 48):
            image.putpixel((x, y), (194, 33, 38, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    colours = brand.raster_colors(buffer.getvalue())

    assert colours and brand.distance(colours[0], RED) < 10


def test_raster_colors_refuses_too_many_pixels_before_decoding(monkeypatch):
    # A flat 3000x3000 PNG is a few KB on the wire and 36 MB once decoded.
    raw = _png(size=(3000, 3000))
    assert len(raw) < brand.MAX_LOGO_BYTES
    converted: List[Any] = []
    monkeypatch.setattr(
        Image.Image, "convert", lambda self, *a, **k: converted.append(self)
    )
    monkeypatch.setattr(
        Image.Image, "load", lambda self: pytest.fail("decoded an oversized logo")
    )

    assert brand.raster_colors(raw) == []
    assert converted == []


def test_raster_colors_shrinks_before_converting(monkeypatch):
    sizes: List[Any] = []
    original = Image.Image.convert

    def spy(self, mode=None, *args, **kwargs):
        if mode == "RGBA":
            sizes.append(self.size)
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", spy)
    brand.raster_colors(_png(size=(1000, 1000)))

    assert sizes and all(max(size) <= 128 for size in sizes)


def test_raster_colors_ignores_undecodable_and_unlisted_formats():
    assert brand.raster_colors(b"not an image at all") == []
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (194, 33, 38)).save(buffer, format="BMP")
    assert brand.raster_colors(buffer.getvalue()) == []


def _fetch_returns(monkeypatch, result: Optional[FetchResult], error=None):
    calls: List[Dict[str, Any]] = []

    async def fake_fetch(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(brand, "fetch_page", fake_fetch)
    return calls


async def test_logo_colors_decodes_off_the_event_loop(monkeypatch):
    raw = _png()
    calls = _fetch_returns(
        monkeypatch,
        FetchResult(url="u", final_url="u", status=200, raw=raw, size_bytes=len(raw)),
    )
    threaded: List[Any] = []

    async def fake_to_thread(fn, *args):
        threaded.append(fn)
        return fn(*args)

    monkeypatch.setattr(brand.asyncio, "to_thread", fake_to_thread)

    colours = await brand.logo_colors("https://cdn.example/logo.png")

    assert threaded == [brand.raster_colors]
    assert colours
    assert calls[0]["decode"] is False
    assert calls[0]["max_bytes"] == brand.MAX_LOGO_BYTES


async def test_logo_colors_reads_svg_fills_without_pillow(monkeypatch):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="#6C5FF9"/></svg>'
    _fetch_returns(
        monkeypatch,
        FetchResult(
            url="u",
            final_url="u",
            status=200,
            headers={"content-type": "image/svg+xml"},
            raw=svg,
        ),
    )
    assert await brand.logo_colors("https://cdn.example/logo.svg") == ["#6c5ff9"]


async def test_logo_colors_skips_truncated_and_failed_reads(monkeypatch):
    _fetch_returns(
        monkeypatch,
        FetchResult(url="u", final_url="u", status=200, raw=_png(), truncated=True),
    )
    assert await brand.logo_colors("https://cdn.example/logo.png") == []

    _fetch_returns(monkeypatch, None, error=FetchFailedError("reset"))
    assert await brand.logo_colors("https://cdn.example/logo.png") == []


# --- Candidates, guards, layering -------------------------------------------


def test_logo_candidates_prefer_structured_data_and_are_https_only():
    profile = _profile(
        json_ld=[{"@type": "Organization", "logo": {"url": "/cdn/logo.png"}}],
        meta={"og:image": "http://store.example/banner.jpg"},
        link_rels=[{"rel": "icon", "href": "https://store.example/favicon.ico"}],
    )
    assert brand.logo_candidates(profile) == [
        "https://store.example/cdn/logo.png",
        "https://store.example/favicon.ico",
    ]
    assert brand.named_logos(profile) == ["https://store.example/cdn/logo.png"]


def test_guards_set_aside_stock_background_and_grey_colours():
    look = BrandLook(
        colors=[
            BrandColor(role="primary", hex="#96bf48", source="page"),
            BrandColor(role="secondary", hex="#fdfdfd", source="page"),
            BrandColor(role="accent", hex="#555555", source="page"),
            BrandColor(role="link", hex=GOLD, source="page"),
        ]
    )
    guarded = brand.apply_guards(look, stock_colors=("#96bf48",))

    assert guarded.color("primary") == GOLD  # promoted from link
    assert {c.hex for c in guarded.alternates} == {"#96bf48", "#fdfdfd", "#555555"}
    assert len(guarded.warnings) == 3


def test_promotion_prefers_the_colour_another_source_agrees_with():
    look = BrandLook(
        colors=[
            BrandColor(role="secondary", hex="#1f6feb", source="page", confidence=0.8),
            BrandColor(role="accent", hex=RED, source="page", confidence=0.5),
        ],
        alternates=[BrandColor(role="primary", hex="#c3222a", source="logo")],
    )
    assert brand.apply_guards(look).color("primary") == RED


def test_merge_first_source_wins_each_role():
    platform = brand.from_platform(GOLD, None, logo_url="https://cdn.example/a.png")
    page = BrandLook(colors=[BrandColor(role="primary", hex=RED, source="page")])
    merged = brand.merge(platform, page)

    assert merged.color("primary") == GOLD
    assert [c.hex for c in merged.alternates] == [RED]
    assert merged.logo_url == "https://cdn.example/a.png"


async def test_resolve_without_firecrawl_falls_back_to_the_logo(monkeypatch):
    monkeypatch.setattr(firecrawl, "FIRECRAWL_API_KEY", "")

    async def logo(url):
        assert url == "https://store.example/logo.png"
        return [RED]

    monkeypatch.setattr(brand, "logo_colors", logo)
    profile = _profile(meta={"og:logo": "https://store.example/logo.png"})

    look = await brand.resolve("https://store.example/", profile)

    assert look.color("primary") == RED
    assert look.logo_url == "https://store.example/logo.png"
    assert "brand provider not configured — skipped" in look.warnings


async def test_a_banner_is_sampled_for_colour_but_never_kept_as_the_logo(
    monkeypatch,
):
    monkeypatch.setattr(firecrawl, "FIRECRAWL_API_KEY", "")
    sampled: List[str] = []

    async def logo(url):
        sampled.append(url)
        return [RED]

    monkeypatch.setattr(brand, "logo_colors", logo)
    profile = _profile(meta={"og:image": "https://store.example/banner.jpg"})

    look = await brand.resolve("https://store.example/", profile)

    assert sampled == ["https://store.example/banner.jpg"]
    assert look.color("primary") == RED
    assert look.logo_url is None


async def test_resolve_with_nothing_found_says_so(monkeypatch):
    monkeypatch.setattr(firecrawl, "FIRECRAWL_API_KEY", "")
    look = await brand.resolve("https://store.example/", _profile())

    assert look.colors == []
    assert "no brand colour could be established" in look.warnings
