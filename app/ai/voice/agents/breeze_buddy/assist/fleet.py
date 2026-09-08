"""Buddy Assist fleet model — pure functions, no I/O.

``build_fleet`` turns the raw rows the ``assist_fleet`` accessors return into
the ``AssistFleetResponse`` the admin console renders. Everything an operator
sees on the fleet page (agent type, generation, health flags, cleanup plan,
shared-block variants, findings) is decided here so it can be unit-tested
without a database.

Vocabulary decisions this module encodes:

* **Agent type** follows the console rule (loom ``agent-type.ts``, user
  decision 2026-07-13): ``'chat'`` in ``supported_channels`` → chat agent (with
  widget voice mode if ``'voice'`` is also present); otherwise a telephony voice
  agent. There is no separate column.
* **Shared block** = the ``## Operating principles`` section of the system
  prompt — the fleet SOP keeps it byte-identical across merchants, so its hash
  is the drift detector. The reference hash comes from the reseller's
  ``buddy-assist-default`` blueprint when it exists, else from an explicitly
  named template, else from the majority of live chat agents.
* **Cleanup** is only ever proposed for chat templates under the assist
  resellers that no widget binds. ``chat_session.template_id`` is
  ``ON DELETE RESTRICT`` (migration 027), so anything with sessions can only be
  deactivated; a hard delete is proposed only at zero sessions.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.ai.voice.agents.breeze_buddy.assist.commerce.assist_onboarding import (
    DEFAULT_ASSIST_TEMPLATE_NAME,
)
from app.schemas.breeze_buddy.admin.assist_fleet import (
    AgentType,
    AssistFleetResponse,
    FleetCleanup,
    FleetFlag,
    FleetIssue,
    FleetMerchant,
    FleetMerchantRow,
    FleetMerchantUsage,
    FleetReference,
    FleetTemplate,
    FleetTotals,
    FleetUsage,
    FleetVariant,
    FleetWidget,
    Generation,
    HostApp,
    Platform,
)

ASSIST_RESELLERS: Tuple[str, ...] = ("BB_SHOPIFY", "BB_ASSIST")
_STANDALONE_PREFIX = "assist-"  # mirrors tenancy._ASSIST_MERCHANT_PREFIX
SHARED_BLOCK_HEADING = "## Operating principles"
_BARE_PROMPT_CHARS = 1300
_SILENT_AFTER_DAYS = 14
_SILENT_CRITICAL_WINDOW_SESSIONS = 100
_SHALLOW_SHARE = 0.5
_IST = ZoneInfo("Asia/Kolkata")

# Origins that never identify a merchant's storefront brand.
_NOISE_ORIGIN_MARKERS = (
    "myshopify.com",
    "localhost",
    "127.0.0.1",
    "breezelabs.app",
    "breezebuddy.ai",
    "breeze.in",
    "juspay.in",
)

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_SHARED_BLOCK_RE = re.compile(
    re.escape(SHARED_BLOCK_HEADING) + r".*?(?=^##\s|\Z)", re.S | re.M
)
_BRAND_PATTERNS = (
    re.compile(r"\*\*Brand:\*\*\s*([^\n|]+)"),
    re.compile(r"You are ([^,\n]+?) Assist\b"),
    re.compile(r"\*\*Assistant name:\*\*\s*([^\n]+)"),
)
_BRAND_SPLIT_RE = re.compile(r"\s+[—–-]\s+|\s\(|[.;:,]\s")


@dataclass
class FleetInputs:
    """Raw rows from the ``assist_fleet`` accessors (see handlers.py)."""

    widgets: List[Dict[str, Any]]
    templates: List[Dict[str, Any]]
    merchants: Dict[str, Dict[str, Any]]  # "reseller|merchant" -> row
    voice_counts: Dict[str, int]  # "reseller|merchant" -> count
    blueprints: List[Dict[str, Any]]
    template_stats: Dict[str, Dict[str, Any]]  # template_id -> row
    template_depth: Dict[str, Dict[str, Any]]  # template_id -> row
    template_daily: Dict[str, Dict[str, int]]  # template_id -> {day: n}
    merchant_stats: Dict[str, Dict[str, Any]]  # "reseller|merchant" -> row
    reference_template_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Small pure helpers (each unit-tested)
# --------------------------------------------------------------------------- #


def normalize_channels(raw: Optional[Iterable[Any]]) -> List[str]:
    """Console rule: unknown/empty channel lists are voice-era templates."""
    seen: List[str] = []
    for c in raw or []:
        s = str(c).strip().lower()
        if s in ("voice", "chat") and s not in seen:
            seen.append(s)
    return seen or ["voice"]


def agent_type_of(channels: Sequence[str]) -> AgentType:
    return "chat" if "chat" in channels else "voice"


def widget_voice_of(channels: Sequence[str]) -> bool:
    return "chat" in channels and "voice" in channels


def prompt_sections(prompt: str) -> List[str]:
    return _SECTION_RE.findall(prompt or "")


def shared_block(prompt: str) -> str:
    m = _SHARED_BLOCK_RE.search(prompt or "")
    return m.group(0).strip() if m else ""


def block_hash(block: str) -> Optional[str]:
    """Stable content hash of a prompt block (``hashlib``, never ``hash()``)."""
    if not block:
        return None
    h = hashlib.sha256()
    h.update(b"breeze-buddy.assist-fleet.shared-block.v1\0")
    h.update(block.encode("utf-8"))
    return h.hexdigest()[:12]


def brand_from_prompt(prompt: str) -> Optional[str]:
    for pat in _BRAND_PATTERNS:
        m = pat.search(prompt or "")
        if not m:
            continue
        b = _BRAND_SPLIT_RE.split(m.group(1).strip().strip("`*"))[0]
        b = b.strip().strip('"“”')
        if b and b.lower() not in ("assist", "the", "a") and len(b) <= 48:
            return b
    return None


def brand_domain(origins: Iterable[str]) -> Optional[str]:
    for o in origins:
        host = re.sub(r"^https?://", "", o).rstrip("/")
        if host and not any(n in host for n in _NOISE_ORIGIN_MARKERS):
            return re.sub(r"^www\.", "", host)
    return None


def merchant_domain(reseller_id: str, merchant_id: str) -> str:
    if reseller_id == "BB_ASSIST" and merchant_id.startswith(_STANDALONE_PREFIX):
        return merchant_id[len(_STANDALONE_PREFIX) :]
    return merchant_id


def host_app_of(reseller_id: str) -> HostApp:
    if reseller_id == "BB_SHOPIFY":
        return "breeze-buddy"
    if reseller_id == "BB_ASSIST":
        return "buddy-assist"
    return "direct"


def platform_of(
    reseller_id: str, merchant_id: str, mcp_servers: Sequence[str]
) -> Platform:
    if reseller_id == "woocommerce":
        return "woocommerce"
    if reseller_id == "acme":
        return "demo"
    if reseller_id == "breeze":
        return "internal"
    if merchant_id.endswith(".myshopify.com") or any(
        "/api/ucp/mcp" in (u or "") for u in mcp_servers
    ):
        return "shopify"
    return "shopify" if reseller_id in ASSIST_RESELLERS else "custom"


def ist_days(now: datetime, window_days: int) -> List[date]:
    """``window_days + 1`` IST calendar days ending today (both ends partial)."""
    today = now.astimezone(_IST).date()
    return [today - timedelta(days=window_days - i) for i in range(window_days + 1)]


# --------------------------------------------------------------------------- #
# Template summaries
# --------------------------------------------------------------------------- #


def summarize_template(
    row: Dict[str, Any],
    *,
    reference_hashes: Sequence[str],
    bound: Optional[Dict[str, Any]] = None,
) -> FleetTemplate:
    flow = row.get("flow") or {}
    conf = row.get("configurations") or {}
    prompt = flow.get("system_prompt") if isinstance(flow, dict) else ""
    prompt = prompt if isinstance(prompt, str) else ""
    llm = conf.get("llm_configurations") if isinstance(conf, dict) else None
    llm = llm if isinstance(llm, dict) else {}
    channels = normalize_channels(row.get("supported_channels"))
    agent_type = agent_type_of(channels)
    sections = prompt_sections(prompt)
    sb_hash = block_hash(shared_block(prompt))
    functions = [
        f.get("name")
        for f in (flow.get("functions") or [])
        if isinstance(f, dict) and f.get("name")
    ]
    mcp_cfg = conf.get("mcp") if isinstance(conf, dict) else None
    mcp_servers = [
        s.get("url")
        for s in ((mcp_cfg or {}).get("servers") or [])
        if isinstance(s, dict) and s.get("url")
    ]
    flavor = conf.get("flavor") if isinstance(conf, dict) else None
    flavor = flavor if isinstance(flavor, dict) else {}
    connectors = sorted(
        {
            c
            for v in flavor.values()
            if isinstance(v, dict)
            for c in (v.get("connectors") or [])
        }
    )
    quick = conf.get("quick_replies") if isinstance(conf, dict) else None
    reseller_id = row.get("reseller_id")
    matches = bool(sb_hash) and sb_hash in reference_hashes

    generation: Generation
    if reseller_id not in ASSIST_RESELLERS:
        generation = "other-tenant"
    elif agent_type == "voice":
        generation = "voice-agent"
    elif sb_hash and matches:
        generation = "standard"
    elif sb_hash:
        generation = "variant"
    elif len(prompt) < _BARE_PROMPT_CHARS:
        generation = "bare"
    else:
        generation = "legacy-personalized"

    return FleetTemplate(
        id=str(row["id"]),
        name=row.get("name") or "",
        reseller_id=reseller_id,
        merchant_id=row.get("merchant_id"),
        is_active=bool(row.get("is_active", True)),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        agent_type=agent_type,
        widget_voice=widget_voice_of(channels),
        channels=channels,
        generation=generation,
        matches_reference=matches,
        prompt_chars=len(prompt),
        sections=sections,
        shared_block_hash=sb_hash,
        model=llm.get("model"),
        provider=llm.get("provider"),
        wismo="get_order_status" in functions
        or any("wismo" in s.lower() for s in sections),
        functions=functions,
        mcp_servers=mcp_servers,
        flavors=sorted(flavor.keys()),
        connectors=connectors,
        quick_replies=len(quick) if isinstance(quick, list) else 0,
        has_greeting=(
            bool(conf.get("initial_greeting")) if isinstance(conf, dict) else False
        ),
        brand_name=brand_from_prompt(prompt),
        bound_widget_config_id=bound["id"] if bound else None,
        bound_merchant_key=(
            f"{bound['reseller_id']}|{bound['merchant_id']}" if bound else None
        ),
    )


def usage_for(
    template_id: str,
    stats: Dict[str, Dict[str, Any]],
    depth: Dict[str, Dict[str, Any]],
    daily: Dict[str, Dict[str, int]],
    days: Sequence[date],
) -> FleetUsage:
    s = stats.get(template_id) or {}
    d = depth.get(template_id) or {}
    per_day = daily.get(template_id) or {}
    sessions = int(d.get("sessions") or 0)
    zero = int(d.get("zero_message_sessions") or 0)
    return FleetUsage(
        total=int(s.get("total") or 0),
        total_window=int(s.get("total_window") or 0),
        total_7d=int(s.get("total_7d") or 0),
        active_now=int(s.get("active_now") or 0),
        last_activity_at=s.get("last_activity_at"),
        first_seen_at=s.get("first_seen_at"),
        avg_messages=(
            round(float(d["avg_messages"]), 1)
            if d.get("avg_messages") is not None
            else None
        ),
        zero_message_share=round(zero / sessions, 2) if sessions else None,
        daily=[int(per_day.get(day.isoformat(), 0)) for day in days],
    )


def cleanup_for(
    template: FleetTemplate, usage: FleetUsage, reference_ids: Sequence[str]
) -> FleetCleanup:
    if template.id in reference_ids:
        return FleetCleanup(
            action="keep",
            reason="This is the fleet reference template; the standard is measured against it.",
        )
    if not template.is_active:
        return FleetCleanup(action="none", reason="Already inactive.")
    if usage.total == 0:
        return FleetCleanup(
            action="delete",
            reason="No chat session references it, so a hard delete is safe.",
        )
    last = usage.last_activity_at.date().isoformat() if usage.last_activity_at else "?"
    return FleetCleanup(
        action="deactivate",
        reason=(
            f"{usage.total:,} chat sessions reference it (last {last}); the "
            "session foreign key blocks a delete, so deactivate to keep transcripts."
        ),
    )


# --------------------------------------------------------------------------- #
# Health flags
# --------------------------------------------------------------------------- #


def flags_for(
    *,
    reseller_id: str,
    origins: Sequence[str],
    widget_active: bool,
    appearance_set: bool,
    merchant_exists: bool,
    template: FleetTemplate,
    usage: FleetUsage,
    now: datetime,
) -> List[FleetFlag]:
    flags: List[FleetFlag] = []
    assist = reseller_id in ASSIST_RESELLERS
    if template.agent_type == "voice":
        flags.append(
            FleetFlag(
                level="critical",
                code="voice-only-template",
                text="Widget is bound to a voice agent (no 'chat' channel); the widget cannot serve it.",
            )
        )
    if not template.is_active:
        flags.append(
            FleetFlag(
                level="critical",
                code="template-inactive",
                text="Bound template is inactive; the widget will fail to start sessions.",
            )
        )
    if not widget_active:
        flags.append(
            FleetFlag(level="info", code="widget-disabled", text="Widget is disabled.")
        )
    if usage.total == 0:
        flags.append(
            FleetFlag(
                level="warning",
                code="never-used",
                text="No chat sessions ever; the widget is provisioned but nobody has opened it.",
            )
        )
    elif usage.last_activity_at and usage.last_activity_at < now - timedelta(
        days=_SILENT_AFTER_DAYS
    ):
        level = (
            "critical"
            if usage.total_window > _SILENT_CRITICAL_WINDOW_SESSIONS
            else "warning"
        )
        since = usage.last_activity_at.date().isoformat()
        text = (
            f"Silent since {since}: {usage.total_window:,} sessions in the window, none in the last 7 days."
            if usage.total_window
            else f"Silent since {since}."
        )
        flags.append(FleetFlag(level=level, code="silent", text=text))
    if assist and not brand_domain(origins):
        flags.append(
            FleetFlag(
                level="warning",
                code="no-custom-domain",
                text="Only the .myshopify.com origin is allowed; shoppers on a custom storefront domain get 403.",
            )
        )
    if template.generation == "bare":
        flags.append(
            FleetFlag(
                level="warning",
                code="bare-template",
                text=f"Live on the {template.prompt_chars}-char default prompt; never personalized.",
            )
        )
    if (
        usage.zero_message_share is not None
        and usage.zero_message_share > _SHALLOW_SHARE
    ):
        flags.append(
            FleetFlag(
                level="info",
                code="shallow-sessions",
                text=f"{usage.zero_message_share:.0%} of window sessions have zero messages.",
            )
        )
    if assist and not merchant_exists:
        flags.append(
            FleetFlag(
                level="critical",
                code="no-merchant-row",
                text="Widget config exists but there is no merchants row for this reseller/merchant.",
            )
        )
    if not appearance_set:
        flags.append(
            FleetFlag(
                level="info",
                code="appearance-default",
                text="Appearance is empty; the widget renders defaults.",
            )
        )
    if template.widget_voice:
        flags.append(
            FleetFlag(
                level="info",
                code="voice-mode",
                text="Chat agent with widget voice mode (WebRTC call inside the widget).",
            )
        )
    return flags


# --------------------------------------------------------------------------- #
# Reference resolution
# --------------------------------------------------------------------------- #


def resolve_reference(
    blueprints: Sequence[Dict[str, Any]],
    templates_by_id: Dict[str, Dict[str, Any]],
    bound_ids: Sequence[str],
    reference_template_id: Optional[str],
) -> FleetReference:
    """Which shared-block hash counts as "standard".

    Blueprint rows (one per reseller) win; an explicitly named template is
    added to them; with neither, the most common shared block among *live*
    chat templates is the reference so the page still shows drift.
    """
    ids: List[str] = []
    hashes: List[str] = []
    for bp in blueprints:
        flow = bp.get("flow") or {}
        prompt = flow.get("system_prompt") if isinstance(flow, dict) else ""
        h = block_hash(shared_block(prompt if isinstance(prompt, str) else ""))
        if h:
            ids.append(str(bp["id"]))
            if h not in hashes:
                hashes.append(h)
    source = "blueprint" if hashes else "none"
    if reference_template_id and reference_template_id in templates_by_id:
        row = templates_by_id[reference_template_id]
        flow = row.get("flow") or {}
        prompt = flow.get("system_prompt") if isinstance(flow, dict) else ""
        h = block_hash(shared_block(prompt if isinstance(prompt, str) else ""))
        if h:
            ids.append(reference_template_id)
            if h not in hashes:
                hashes.append(h)
            source = "blueprint" if source == "blueprint" else "template"
    if not hashes:
        counts: Counter = Counter()
        for tid in bound_ids:
            row = templates_by_id.get(tid)
            if not row or row.get("reseller_id") not in ASSIST_RESELLERS:
                continue
            flow = row.get("flow") or {}
            prompt = flow.get("system_prompt") if isinstance(flow, dict) else ""
            h = block_hash(shared_block(prompt if isinstance(prompt, str) else ""))
            if h:
                counts[h] += 1
        if counts:
            top, _ = counts.most_common(1)[0]
            hashes = [top]
            source = "majority"
    return FleetReference(source=source, template_ids=ids, hashes=hashes)


# --------------------------------------------------------------------------- #
# The builder
# --------------------------------------------------------------------------- #


def build_fleet(
    inputs: FleetInputs, *, now: Optional[datetime] = None, window_days: int = 30
) -> AssistFleetResponse:
    now = now or datetime.now(timezone.utc)
    days = ist_days(now, window_days)
    templates_by_id = {str(t["id"]): t for t in inputs.templates}
    widgets_by_tid = {str(w["template_id"]): w for w in inputs.widgets}
    bound_ids = list(widgets_by_tid.keys())

    reference = resolve_reference(
        inputs.blueprints, templates_by_id, bound_ids, inputs.reference_template_id
    )

    summaries: Dict[str, FleetTemplate] = {}
    for row in inputs.templates:
        tid = str(row["id"])
        summaries[tid] = summarize_template(
            row, reference_hashes=reference.hashes, bound=widgets_by_tid.get(tid)
        )

    # ---- merchants (one per widget) --------------------------------------
    merchants: List[FleetMerchant] = []
    for w in inputs.widgets:
        key = f"{w['reseller_id']}|{w['merchant_id']}"
        tid = str(w["template_id"])
        tsum = summaries.get(tid)
        if tsum is None:
            # Bound template vanished between the two reads; describe it minimally.
            tsum = summarize_template(
                {
                    "id": tid,
                    "name": "(missing template)",
                    "reseller_id": w["reseller_id"],
                },
                reference_hashes=reference.hashes,
                bound=w,
            )
            tsum.is_active = False
        usage = usage_for(
            tid,
            inputs.template_stats,
            inputs.template_depth,
            inputs.template_daily,
            days,
        )
        mrow = inputs.merchants.get(key)
        mstats = inputs.merchant_stats.get(key) or {}
        origins = list(w.get("allowed_origins") or [])
        appearance_set = bool(w.get("appearance"))
        flags = flags_for(
            reseller_id=w["reseller_id"],
            origins=origins,
            widget_active=bool(w.get("active", True)),
            appearance_set=appearance_set,
            merchant_exists=mrow is not None,
            template=tsum,
            usage=usage,
            now=now,
        )
        domain = merchant_domain(w["reseller_id"], w["merchant_id"])
        bdomain = brand_domain(origins)
        merchants.append(
            FleetMerchant(
                key=key,
                reseller_id=w["reseller_id"],
                merchant_id=w["merchant_id"],
                merchant_domain=domain,
                host_app=host_app_of(w["reseller_id"]),
                platform=platform_of(
                    w["reseller_id"], w["merchant_id"], tsum.mcp_servers
                ),
                brand=bdomain or tsum.brand_name or domain.split(".")[0],
                brand_domain=bdomain,
                origins=origins,
                widget=FleetWidget(
                    id=str(w["id"]),
                    active=bool(w.get("active", True)),
                    created_at=w.get("created_at"),
                    updated_at=w.get("updated_at"),
                    appearance_set=appearance_set,
                    max_sessions_per_ip_hour=w.get("max_sessions_per_ip_hour"),
                    max_messages_per_ip_hour=w.get("max_messages_per_ip_hour"),
                    max_concurrent_per_ip=w.get("max_concurrent_per_ip"),
                    max_voice_sessions_per_ip_hour=w.get(
                        "max_voice_sessions_per_ip_hour"
                    ),
                ),
                merchant_row=(
                    FleetMerchantRow(
                        exists=True,
                        name=mrow.get("name"),
                        is_active=mrow.get("is_active"),
                        created_at=mrow.get("created_at"),
                    )
                    if mrow
                    else FleetMerchantRow(exists=False)
                ),
                template=tsum,
                usage=usage,
                merchant_usage=FleetMerchantUsage(
                    total=int(mstats.get("total") or 0),
                    total_window=int(mstats.get("total_window") or 0),
                    total_7d=int(mstats.get("total_7d") or 0),
                    last_activity_at=mstats.get("last_activity_at"),
                ),
                voice_templates=int(inputs.voice_counts.get(key, 0)),
                flags=flags,
            )
        )
    merchants.sort(
        key=lambda m: (
            m.reseller_id not in ASSIST_RESELLERS,
            -m.usage.total_window,
            -m.usage.total,
        )
    )

    # ---- templates: usage + cleanup for orphans ---------------------------
    templates: List[FleetTemplate] = []
    for tid, tsum in summaries.items():
        tsum.usage = usage_for(
            tid,
            inputs.template_stats,
            inputs.template_depth,
            inputs.template_daily,
            days,
        )
        orphan = (
            tsum.bound_widget_config_id is None
            and tsum.agent_type == "chat"
            and tsum.reseller_id in ASSIST_RESELLERS
        )
        if orphan:
            tsum.cleanup = cleanup_for(tsum, tsum.usage, reference.template_ids)
        templates.append(tsum)
    templates.sort(
        key=lambda t: (
            t.generation in ("voice-agent", "other-tenant"),
            t.bound_widget_config_id is None,
            t.name.lower(),
        )
    )

    # ---- variants ---------------------------------------------------------
    by_hash: Dict[str, List[FleetTemplate]] = defaultdict(list)
    for t in templates:
        if t.shared_block_hash and t.generation not in ("voice-agent", "other-tenant"):
            by_hash[t.shared_block_hash].append(t)
    variants = [
        FleetVariant(
            hash=h,
            is_reference=h in reference.hashes,
            template_ids=[t.id for t in ts],
            live_count=sum(1 for t in ts if t.bound_widget_config_id),
        )
        for h, ts in by_hash.items()
    ]
    variants.sort(key=lambda v: (not v.is_reference, -len(v.template_ids)))

    # ---- totals -----------------------------------------------------------
    assist_merchants = [m for m in merchants if m.reseller_id in ASSIST_RESELLERS]
    orphans = [t for t in templates if t.cleanup is not None]
    totals = FleetTotals(
        merchants=len(merchants),
        active_7d=sum(1 for m in merchants if m.usage.total_7d > 0),
        sessions_total=sum(m.usage.total for m in merchants),
        sessions_window=sum(m.usage.total_window for m in merchants),
        sessions_7d=sum(m.usage.total_7d for m in merchants),
        silent=sum(
            1
            for m in merchants
            if any(f.code in ("silent", "never-used") for f in m.flags)
        ),
        orphans=len(orphans),
        orphans_deletable=sum(
            1 for t in orphans if t.cleanup and t.cleanup.action == "delete"
        ),
        standard=sum(1 for m in merchants if m.template.generation == "standard"),
        bare=sum(1 for m in merchants if m.template.generation == "bare"),
        wismo=sum(1 for m in merchants if m.template.wismo),
        voice_templates=sum(m.voice_templates for m in merchants),
        by_reseller=dict(Counter(m.reseller_id for m in merchants)),
    )

    issues = _issues(
        merchants=merchants,
        assist_merchants=assist_merchants,
        orphans=orphans,
        variants=variants,
        blueprints=inputs.blueprints,
        reference=reference,
    )

    return AssistFleetResponse(
        generated_at=now,
        window_days=window_days,
        days=[d.isoformat() for d in days],
        assist_resellers=list(ASSIST_RESELLERS),
        reference=reference,
        merchants=merchants,
        templates=templates,
        variants=variants,
        issues=issues,
        totals=totals,
    )


def _issues(
    *,
    merchants: List[FleetMerchant],
    assist_merchants: List[FleetMerchant],
    orphans: List[FleetTemplate],
    variants: List[FleetVariant],
    blueprints: Sequence[Dict[str, Any]],
    reference: FleetReference,
) -> List[FleetIssue]:
    issues: List[FleetIssue] = []
    have_bp = {bp.get("reseller_id") for bp in blueprints}
    for r in ASSIST_RESELLERS:
        if r not in have_bp:
            issues.append(
                FleetIssue(
                    level="critical",
                    title=f"Blueprint template '{DEFAULT_ASSIST_TEMPLATE_NAME}' is missing under {r}",
                    detail=(
                        "The bare onboard path loads this reseller-level template on first "
                        "create, so every fresh install through that reseller fails with "
                        "DEFAULT_TEMPLATE_NOT_FOUND until it exists."
                    ),
                    fix=f"Create a reseller-level (merchant_id null) template named "
                    f"'{DEFAULT_ASSIST_TEMPLATE_NAME}' under {r}.",
                )
            )
    for m in assist_merchants:
        if any(f.code == "silent" and f.level == "critical" for f in m.flags):
            since = (
                m.usage.last_activity_at.date().isoformat()
                if m.usage.last_activity_at
                else "?"
            )
            issues.append(
                FleetIssue(
                    level="critical",
                    title=f"{m.brand} went silent on {since}",
                    detail=(
                        f"{m.usage.total:,} sessions lifetime, {m.usage.total_window:,} in the "
                        "window, none in the last 7 days."
                    ),
                    fix="Check the theme app-embed on the storefront and that the storefront-config resolve returns 200 for its domain.",
                )
            )
    misbound = [
        m for m in merchants if any(f.code == "voice-only-template" for f in m.flags)
    ]
    if misbound:
        issues.append(
            FleetIssue(
                level="critical",
                title=f"{len(misbound)} widget(s) bound to voice-only templates",
                detail=", ".join(m.brand for m in misbound[:8]),
                fix="Add the 'chat' channel to the template or bind the widget to a chat agent.",
            )
        )
    if orphans:
        deletable = sum(
            1 for t in orphans if t.cleanup and t.cleanup.action == "delete"
        )
        issues.append(
            FleetIssue(
                level="warning",
                title=f"{len(orphans)} orphan chat templates under the assist resellers",
                detail=(
                    f"{deletable} have no sessions and can be deleted; the rest should be "
                    "deactivated (session foreign key blocks a delete)."
                ),
                fix="Use the cleanup plan.",
            )
        )
    missing_rows = [m for m in assist_merchants if not m.merchant_row.exists]
    if missing_rows:
        issues.append(
            FleetIssue(
                level="warning",
                title=f"{len(missing_rows)} assist widget(s) have no merchants row",
                detail=", ".join(m.merchant_id for m in missing_rows[:8]),
                fix="Create the merchants row so scoped users can be granted access.",
            )
        )
    if assist_merchants:
        standard = sum(
            1 for m in assist_merchants if m.template.generation == "standard"
        )
        bare = sum(1 for m in assist_merchants if m.template.generation == "bare")
        live_variants = sum(1 for v in variants if v.live_count and not v.is_reference)
        issues.append(
            FleetIssue(
                level="info",
                title=f"{standard} of {len(assist_merchants)} live assist agents match the reference prompt",
                detail=(
                    f"{live_variants} other shared-block variants are live; {bare} agents still "
                    f"run the bare default prompt. Reference source: {reference.source}."
                ),
                fix="Pick the canonical shared block, then roll it out with the fleet SOP.",
            )
        )
    return issues


__all__ = [
    "ASSIST_RESELLERS",
    "FleetInputs",
    "agent_type_of",
    "block_hash",
    "brand_domain",
    "brand_from_prompt",
    "build_fleet",
    "cleanup_for",
    "flags_for",
    "host_app_of",
    "ist_days",
    "merchant_domain",
    "normalize_channels",
    "platform_of",
    "prompt_sections",
    "resolve_reference",
    "shared_block",
    "summarize_template",
    "usage_for",
    "widget_voice_of",
]
