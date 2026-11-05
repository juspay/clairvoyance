"""The engine never names a platform, and never speaks a vertical's vocabulary.

ASSIST-ENGINE-DESIGN.md §1: platform facts live in ``assist/platforms/<name>/``
only; the engine, the onboarding surface and the assist HTTP surface are
platform-blind. The same scopes are also vertical-blind — a booking or transit
assistant must run on them unchanged — so commerce words belong to
``assist/commerce/`` (the vertical), never to the engine or the surfaces.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
ENGINE = ROOT / "app/ai/voice/agents/breeze_buddy/assist/engine"
PLATFORM_BLIND = (
    ENGINE,
    ROOT / "app/ai/voice/agents/breeze_buddy/assist/onboarding",
    ROOT / "app/api/routers/breeze_buddy/assist",
)
PLATFORM_WORDS = re.compile(r"shopify|woocommerce|magento|bigcommerce", re.IGNORECASE)
VERTICAL_WORDS = re.compile(
    r"\b(checkout|cart|product|products|collection|collections|sizing|catalog|catalogue)\b",
    re.IGNORECASE,
)


def _offenders(scopes, pattern):
    found = []
    for scope in scopes:
        for path in scope.rglob("*.py"):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    return found


def test_engine_onboarding_and_routes_name_no_platform():
    offenders = _offenders(PLATFORM_BLIND, PLATFORM_WORDS)
    assert not offenders, "platform names outside platforms/:\n" + "\n".join(offenders)


def test_engine_onboarding_and_routes_have_no_vertical_vocabulary():
    offenders = _offenders(PLATFORM_BLIND, VERTICAL_WORDS)
    assert not offenders, "vertical vocabulary outside the verticals:\n" + "\n".join(
        offenders
    )
