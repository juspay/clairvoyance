"""``GET /admin/assist-fleet``: query builders, the pure fleet model, and the route gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.assist import fleet
from app.api.routers.breeze_buddy.admin.assist_fleet import handlers, router
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.database.queries.breeze_buddy.admin import assist_fleet as q
from app.schemas import UserInfo, UserRole

NOW = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
CANON = "## Operating principles\n\nWarm, precise, never pushy.\n"
OTHER_BLOCK = "## Operating principles\n\nSomething else entirely.\n"
WISMO = "## Order tracking (WISMO)\n\nAsk for the order number.\n"


def _prompt(brand: str = "Acme", block: str = CANON, tail: str = "") -> str:
    return f"## Brand identity\n\n- **Brand:** {brand} (est. 1966)\n\n{block}{tail}"


def _template(
    tid: str,
    *,
    reseller: str = "BB_SHOPIFY",
    merchant: Optional[str] = "m1.myshopify.com",
    channels: Sequence[str] = ("chat", "voice"),
    prompt: str = "",
    model: str = "gemini-2.5-flash",
    functions: Sequence[str] = (),
    mcp: Sequence[str] = ("https://{shop_url}/api/ucp/mcp",),
    active: bool = True,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": tid,
        "reseller_id": reseller,
        "merchant_id": merchant,
        "name": name or f"{tid}-assist",
        "is_active": active,
        "supported_channels": list(channels),
        "flow": {
            "system_prompt": prompt,
            "functions": [{"name": f} for f in functions],
        },
        "configurations": {
            "llm_configurations": {"model": model, "provider": "google_vertex"},
            "mcp": {"servers": [{"url": u} for u in mcp]},
            "quick_replies": ["a", "b"],
            "initial_greeting": "Hi",
        },
        "created_at": NOW - timedelta(days=40),
        "updated_at": NOW - timedelta(days=1),
    }


def _widget(
    wid: str,
    tid: str,
    *,
    reseller: str = "BB_SHOPIFY",
    merchant: str = "m1.myshopify.com",
    origins: Optional[List[str]] = None,
    active: bool = True,
    appearance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "id": wid,
        "reseller_id": reseller,
        "merchant_id": merchant,
        "template_id": tid,
        "allowed_origins": origins if origins is not None else [f"https://{merchant}"],
        "max_sessions_per_ip_hour": 60,
        "max_messages_per_ip_hour": 600,
        "max_concurrent_per_ip": 4,
        "max_voice_sessions_per_ip_hour": 10,
        "active": active,
        "appearance": appearance or {},
        "created_at": NOW - timedelta(days=30),
        "updated_at": NOW - timedelta(days=30),
    }


def _stats(total: int, window: int = 0, d7: int = 0, last_days_ago: float = 0.5):
    return {
        "total": total,
        "total_window": window,
        "total_7d": d7,
        "active_now": 0,
        "last_activity_at": NOW - timedelta(days=last_days_ago),
        "first_seen_at": NOW - timedelta(days=60),
    }


def _inputs(**over: Any) -> fleet.FleetInputs:
    base: Dict[str, Any] = dict(
        widgets=[],
        templates=[],
        merchants={},
        voice_counts={},
        blueprints=[],
        template_stats={},
        template_depth={},
        template_daily={},
        merchant_stats={},
        reference_template_id=None,
    )
    base.update(over)
    return fleet.FleetInputs(**base)


def _blueprint(reseller: str, block: str = CANON) -> Dict[str, Any]:
    return {
        "id": f"bp-{reseller}",
        "reseller_id": reseller,
        "flow": {"system_prompt": "{{brand_identity_section}}\n\n" + block},
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def test_agent_type_follows_the_console_rule():
    assert fleet.normalize_channels(None) == ["voice"]
    assert fleet.normalize_channels(["CHAT", " voice ", "chat", "sms"]) == [
        "chat",
        "voice",
    ]
    assert fleet.agent_type_of(["voice"]) == "voice"
    assert fleet.agent_type_of(["chat"]) == "chat"
    assert fleet.agent_type_of(["chat", "voice"]) == "chat"
    assert fleet.widget_voice_of(["chat", "voice"]) is True
    assert fleet.widget_voice_of(["chat"]) is False
    assert fleet.widget_voice_of(["voice"]) is False


def test_shared_block_extraction_and_hash():
    prompt = _prompt(block=CANON, tail=WISMO)
    assert fleet.shared_block(prompt) == CANON.strip()
    assert fleet.prompt_sections(prompt) == [
        "Brand identity",
        "Operating principles",
        "Order tracking (WISMO)",
    ]
    h1 = fleet.block_hash(fleet.shared_block(prompt))
    h2 = fleet.block_hash(fleet.shared_block(_prompt(brand="Other", block=CANON)))
    assert h1 is not None and h1 == h2 and len(h1) == 12
    assert fleet.block_hash(fleet.shared_block(_prompt(block=OTHER_BLOCK))) != h1
    assert fleet.block_hash("") is None
    assert fleet.shared_block("no headings here") == ""


def test_brand_and_domain_helpers():
    assert fleet.brand_from_prompt(_prompt(brand="Amir & Sons")) == "Amir & Sons"
    assert fleet.brand_from_prompt("You are zuvio Assist, a helper.") == "zuvio"
    assert fleet.brand_from_prompt("You are Assist, a helper.") is None
    assert (
        fleet.brand_domain(
            ["https://x.myshopify.com", "http://localhost:5173", "https://www.shop.in"]
        )
        == "shop.in"
    )
    assert fleet.brand_domain(["https://x.myshopify.com"]) is None
    assert fleet.merchant_domain("BB_ASSIST", "assist-shop.in") == "shop.in"
    assert fleet.merchant_domain("BB_SHOPIFY", "assist-shop.in") == "assist-shop.in"
    assert fleet.host_app_of("BB_SHOPIFY") == "breeze-buddy"
    assert fleet.host_app_of("BB_ASSIST") == "buddy-assist"
    assert fleet.host_app_of("breeze") == "direct"
    assert fleet.platform_of("acme", "showcase_x", []) == "demo"
    assert fleet.platform_of("breeze", "lohono", []) == "internal"
    assert fleet.platform_of("woocommerce", "shopyvision", []) == "woocommerce"
    assert fleet.platform_of("BB_ASSIST", "assist-shop.in", []) == "shopify"
    assert fleet.platform_of("other", "x", ["https://a/api/ucp/mcp"]) == "shopify"
    assert fleet.platform_of("other", "x", []) == "custom"


def test_ist_days_cover_window_plus_today():
    days = fleet.ist_days(NOW, 30)
    assert len(days) == 31
    assert days[-1].isoformat() == "2026-09-08"
    assert days[0].isoformat() == "2026-08-09"


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def test_generation_classification_against_blueprint_reference():
    templates = [
        _template("std", prompt=_prompt(block=CANON)),
        _template("var", prompt=_prompt(block=OTHER_BLOCK), model="gemini-3.6-flash"),
        _template("bare", prompt="You are X Assist. Be brief."),
        _template("legacy", prompt="You are X Assist. " + "Long text. " * 200),
        _template("voice", channels=("voice",), prompt=""),
        _template("demo", reseller="acme", merchant="showcase_x", prompt=_prompt()),
    ]
    widgets = [
        _widget(
            f"w-{t['id']}",
            t["id"],
            reseller=t["reseller_id"],
            merchant=t["merchant_id"],
        )
        for t in templates
    ]
    out = fleet.build_fleet(
        _inputs(
            widgets=widgets,
            templates=templates,
            blueprints=[_blueprint("BB_SHOPIFY"), _blueprint("BB_ASSIST")],
        ),
        now=NOW,
    )
    gen = {t.id: t.generation for t in out.templates}
    assert gen == {
        "std": "standard",
        "var": "variant",
        "bare": "bare",
        "legacy": "legacy-personalized",
        "voice": "voice-agent",
        "demo": "other-tenant",
    }
    assert out.reference.source == "blueprint"
    assert out.reference.template_ids == ["bp-BB_SHOPIFY", "bp-BB_ASSIST"]
    assert len(out.reference.hashes) == 1
    by_id = {t.id: t for t in out.templates}
    assert by_id["std"].matches_reference is True
    assert by_id["var"].matches_reference is False
    # Variants: canon (reference, 1 live) + the drifted block (1 live).
    assert [(v.is_reference, v.live_count) for v in out.variants] == [
        (True, 1),
        (False, 1),
    ]
    # Nothing outside the assist resellers is graded or clustered.
    assert all(
        "demo" not in v.template_ids and "voice" not in v.template_ids
        for v in out.variants
    )


def test_reference_falls_back_to_majority_of_live_agents():
    templates = [
        _template("a", merchant="a.myshopify.com", prompt=_prompt(block=OTHER_BLOCK)),
        _template("b", merchant="b.myshopify.com", prompt=_prompt(block=OTHER_BLOCK)),
        _template("c", merchant="c.myshopify.com", prompt=_prompt(block=CANON)),
        _template("orphan", merchant="d.myshopify.com", prompt=_prompt(block=CANON)),
    ]
    widgets = [
        _widget(f"w-{t['id']}", t["id"], merchant=t["merchant_id"])
        for t in templates
        if t["id"] != "orphan"
    ]
    out = fleet.build_fleet(_inputs(widgets=widgets, templates=templates), now=NOW)
    assert out.reference.source == "majority"
    gen = {t.id: t.generation for t in out.templates}
    assert gen["a"] == gen["b"] == "standard"
    assert gen["c"] == "variant"
    assert gen["orphan"] == "variant"


def test_explicit_reference_template_wins_over_majority():
    templates = [
        _template("a", merchant="a.myshopify.com", prompt=_prompt(block=OTHER_BLOCK)),
        _template("b", merchant="b.myshopify.com", prompt=_prompt(block=OTHER_BLOCK)),
        _template("ref", merchant="c.myshopify.com", prompt=_prompt(block=CANON)),
    ]
    widgets = [
        _widget(f"w-{t['id']}", t["id"], merchant=t["merchant_id"]) for t in templates
    ]
    out = fleet.build_fleet(
        _inputs(widgets=widgets, templates=templates, reference_template_id="ref"),
        now=NOW,
    )
    assert out.reference.source == "template"
    assert out.reference.template_ids == ["ref"]
    gen = {t.id: t.generation for t in out.templates}
    assert gen == {"a": "variant", "b": "variant", "ref": "standard"}


# --------------------------------------------------------------------------- #
# orphans + cleanup
# --------------------------------------------------------------------------- #


def test_orphan_cleanup_plan():
    templates = [
        _template("live", prompt=_prompt()),
        _template("dead", prompt=_prompt(), name="old-1"),
        _template("used", prompt=_prompt(), name="old-2"),
        _template("off", prompt=_prompt(), active=False, name="old-3"),
        _template("voice", channels=("voice",), name="order-confirmation"),
        _template("ref", prompt=_prompt(), name="reference"),
        _template("demo", reseller="acme", merchant="showcase_x", prompt=_prompt()),
    ]
    widgets = [_widget("w-live", "live")]
    out = fleet.build_fleet(
        _inputs(
            widgets=widgets,
            templates=templates,
            template_stats={"used": _stats(12, last_days_ago=30)},
            reference_template_id="ref",
        ),
        now=NOW,
    )
    plan = {t.id: (t.cleanup.action if t.cleanup else None) for t in out.templates}
    assert plan == {
        "live": None,  # bound: never in the cleanup plan
        "dead": "delete",  # zero sessions
        "used": "deactivate",  # sessions reference it
        "off": "none",  # already inactive
        "voice": None,  # telephony template, not a chat orphan
        "ref": "keep",  # the reference
        "demo": None,  # outside the assist resellers
    }
    used = next(t for t in out.templates if t.id == "used")
    assert used.cleanup is not None
    assert "12" in used.cleanup.reason and "deactivate" in used.cleanup.reason
    assert out.totals.orphans == 4  # dead, used, off, ref
    assert out.totals.orphans_deletable == 1


# --------------------------------------------------------------------------- #
# flags + usage + issues
# --------------------------------------------------------------------------- #


def test_merchant_flags_and_usage():
    templates = [
        _template("silent", merchant="s.myshopify.com", prompt=_prompt()),
        _template("fresh", merchant="f.myshopify.com", prompt=_prompt()),
        _template("bare", merchant="b.myshopify.com", prompt="You are B Assist."),
        _template("voice", merchant="v.myshopify.com", channels=("voice",)),
        _template("demo", reseller="acme", merchant="showcase_x", prompt=_prompt()),
    ]
    widgets = [
        _widget(
            "w-silent",
            "silent",
            merchant="s.myshopify.com",
            origins=["https://s.myshopify.com", "https://silent.in"],
        ),
        _widget("w-fresh", "fresh", merchant="f.myshopify.com"),
        _widget(
            "w-bare",
            "bare",
            merchant="b.myshopify.com",
            appearance={"primary_color": "#000"},
        ),
        _widget("w-voice", "voice", merchant="v.myshopify.com"),
        _widget("w-demo", "demo", reseller="acme", merchant="showcase_x", origins=[]),
    ]
    days = fleet.ist_days(NOW, 30)
    out = fleet.build_fleet(
        _inputs(
            widgets=widgets,
            templates=templates,
            merchants={
                "BB_SHOPIFY|s.myshopify.com": {"name": "S", "is_active": True},
                "BB_SHOPIFY|f.myshopify.com": {"name": "F", "is_active": True},
                "BB_SHOPIFY|b.myshopify.com": {"name": "B", "is_active": True},
                "BB_SHOPIFY|v.myshopify.com": {"name": "V", "is_active": True},
            },
            template_stats={
                "silent": _stats(24_000, window=7_000, d7=0, last_days_ago=15),
                "bare": _stats(50, window=50, d7=10),
            },
            template_depth={
                "bare": {
                    "sessions": 50,
                    "avg_messages": 0.8,
                    "zero_message_sessions": 40,
                }
            },
            template_daily={"bare": {days[-1].isoformat(): 7, days[-2].isoformat(): 3}},
            voice_counts={"BB_SHOPIFY|s.myshopify.com": 2},
            blueprints=[_blueprint("BB_SHOPIFY"), _blueprint("BB_ASSIST")],
        ),
        now=NOW,
    )
    by = {m.merchant_id: m for m in out.merchants}
    codes = lambda m: {f.code: f.level for f in m.flags}  # noqa: E731

    silent = by["s.myshopify.com"]
    assert codes(silent)["silent"] == "critical"
    assert silent.brand == "silent.in" and silent.brand_domain == "silent.in"
    assert silent.voice_templates == 2
    assert silent.template.generation == "standard"

    fresh = by["f.myshopify.com"]
    assert codes(fresh)["never-used"] == "warning"
    assert codes(fresh)["no-custom-domain"] == "warning"
    assert fresh.brand == "Acme"  # from the prompt when no custom origin exists

    bare = by["b.myshopify.com"]
    assert codes(bare)["bare-template"] == "warning"
    assert codes(bare)["shallow-sessions"] == "info"
    assert "appearance-default" not in codes(bare)
    assert bare.usage.avg_messages == 0.8 and bare.usage.zero_message_share == 0.8
    assert len(bare.usage.daily) == 31 and bare.usage.daily[-1] == 7
    assert bare.usage.daily[-2] == 3 and sum(bare.usage.daily) == 10

    voice = by["v.myshopify.com"]
    assert codes(voice)["voice-only-template"] == "critical"

    demo = by["showcase_x"]
    assert demo.platform == "demo" and demo.host_app == "direct"
    assert "no-merchant-row" not in codes(demo)  # only enforced for assist resellers
    assert "no-custom-domain" not in codes(demo)

    titles = [i.title for i in out.issues]
    assert any("went silent" in t for t in titles)
    assert any("voice-only" in t for t in titles)
    assert not any("Blueprint" in t for t in titles)
    assert (
        out.totals.merchants == 5 and out.totals.silent == 4
    )  # 1 silent + 3 never-used
    assert out.totals.by_reseller == {"BB_SHOPIFY": 4, "acme": 1}
    # Assist merchants sort first, then by window volume.
    assert [m.merchant_id for m in out.merchants][:2] == [
        "s.myshopify.com",
        "b.myshopify.com",
    ]


def test_missing_blueprints_and_missing_merchant_rows_are_findings():
    templates = [_template("t", prompt=_prompt())]
    out = fleet.build_fleet(
        _inputs(widgets=[_widget("w", "t")], templates=templates), now=NOW
    )
    crit = [i.title for i in out.issues if i.level == "critical"]
    assert any("missing under BB_SHOPIFY" in t for t in crit)
    assert any("missing under BB_ASSIST" in t for t in crit)
    m = out.merchants[0]
    assert {f.code for f in m.flags} >= {"no-merchant-row", "never-used"}
    assert any("no merchants row" in i.title for i in out.issues)


def test_bound_template_missing_from_read_is_described_not_fatal():
    out = fleet.build_fleet(_inputs(widgets=[_widget("w", "gone")]), now=NOW)
    m = out.merchants[0]
    assert m.template.name == "(missing template)"
    assert m.template.is_active is False
    assert {f.code for f in m.flags} >= {"template-inactive"}


# --------------------------------------------------------------------------- #
# query builders
# --------------------------------------------------------------------------- #


def test_query_builders_bind_in_order():
    since = NOW - timedelta(days=30)
    since7 = NOW - timedelta(days=7)
    query, values = q.chat_templates_query(["BB_SHOPIFY"], ["t1", "t2"])
    assert "'chat' = ANY(supported_channels)" in query
    assert "id = ANY($2::uuid[])" in query
    assert values == [["BB_SHOPIFY"], ["t1", "t2"]]

    query, values = q.template_session_stats_query(["t1"], since, since7)
    assert "cs.template_id = ANY($1::uuid[])" in query
    assert "created_at >= $2::timestamptz" in query
    assert "created_at >= $3::timestamptz" in query
    assert values == [["t1"], since, since7]

    query, values = q.template_session_depth_query(["t1"], since)
    assert "FROM chat_message m" in query and "msgs = 0" in query
    assert values == [["t1"], since]

    query, values = q.template_daily_sessions_query(["t1"], since)
    assert "AT TIME ZONE 'Asia/Kolkata'" in query
    assert values == [["t1"], since]

    query, values = q.merchant_session_stats_query(["r"], ["m"], since, since7)
    assert "cs.reseller_id = ANY($1::text[])" in query
    assert "cs.merchant_id = ANY($2::text[])" in query
    assert values == [["r"], ["m"], since, since7]

    query, values = q.voice_template_counts_query(["BB_SHOPIFY", "BB_ASSIST"])
    assert "NOT ('chat' = ANY(supported_channels))" in query
    assert values == [["BB_SHOPIFY", "BB_ASSIST"]]

    query, values = q.blueprint_templates_query(["BB_SHOPIFY"], "buddy-assist-default")
    assert "merchant_id IS NULL" in query and "name = $2" in query
    assert values == [["BB_SHOPIFY"], "buddy-assist-default"]

    query, values = q.all_widget_configs_query()
    assert "public_widget_key" not in query
    assert values == []


# --------------------------------------------------------------------------- #
# route
# --------------------------------------------------------------------------- #

ADMIN = UserInfo(id="admin-1", username="admin", role=UserRole.ADMIN)
RESELLER = UserInfo(
    id="r-1", username="partner", role=UserRole.RESELLER, reseller_ids=["BB_SHOPIFY"]
)


@pytest.fixture()
def fleet_mocks(monkeypatch) -> Dict[str, AsyncMock]:
    """Patch every accessor *as imported into the handler module* (repo idiom)."""
    templates = [_template("t", prompt=_prompt())]
    mocks: Dict[str, AsyncMock] = {
        "fetch_all_widget_configs": AsyncMock(return_value=[_widget("w", "t")]),
        "fetch_chat_templates": AsyncMock(return_value=templates),
        "fetch_merchants_by_ids": AsyncMock(return_value={}),
        "fetch_voice_template_counts": AsyncMock(return_value={}),
        "fetch_blueprint_templates": AsyncMock(
            return_value=[_blueprint("BB_SHOPIFY"), _blueprint("BB_ASSIST")]
        ),
        "fetch_template_session_stats": AsyncMock(
            return_value={"t": _stats(3, window=3, d7=1)}
        ),
        "fetch_template_session_depth": AsyncMock(return_value={}),
        "fetch_template_daily_sessions": AsyncMock(return_value={}),
        "fetch_merchant_session_stats": AsyncMock(return_value={}),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(handlers, name, mock)
    return mocks


@pytest.fixture()
def fleet_app(fleet_mocks):
    def make(user: UserInfo) -> TestClient:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_with_rbac] = lambda: user
        return TestClient(app)

    return make


def test_admin_gets_the_fleet(fleet_app, fleet_mocks):
    res = fleet_app(ADMIN).get("/admin/assist-fleet?window_days=14")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["window_days"] == 14 and len(body["days"]) == 15
    assert body["assist_resellers"] == ["BB_SHOPIFY", "BB_ASSIST"]
    assert body["reference"]["source"] == "blueprint"
    assert (
        len(body["merchants"]) == 1
        and body["merchants"][0]["template"]["generation"] == "standard"
    )
    assert body["merchants"][0]["usage"]["total"] == 3
    assert body["totals"]["merchants"] == 1 and body["totals"]["active_7d"] == 1
    # The reference template ids are forwarded to the template read so an explicit
    # reference outside the bound set is still loaded.
    fleet_mocks["fetch_chat_templates"].assert_awaited_once()
    assert fleet_mocks["fetch_chat_templates"].await_args.args[1] == ["t"]


def test_reference_template_id_is_loaded_with_the_bound_set(fleet_app, fleet_mocks):
    res = fleet_app(ADMIN).get("/admin/assist-fleet?reference_template_id=ref-x")
    assert res.status_code == 200
    assert fleet_mocks["fetch_chat_templates"].await_args.args[1] == ["t", "ref-x"]


def test_non_admin_is_refused(fleet_app, fleet_mocks):
    res = fleet_app(RESELLER).get("/admin/assist-fleet")
    assert res.status_code == 403
    fleet_mocks["fetch_all_widget_configs"].assert_not_awaited()


def test_window_days_is_bounded(fleet_app):
    assert fleet_app(ADMIN).get("/admin/assist-fleet?window_days=3").status_code == 422
    assert (
        fleet_app(ADMIN).get("/admin/assist-fleet?window_days=400").status_code == 422
    )
