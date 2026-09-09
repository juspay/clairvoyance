"""The engine never names a platform (ASSIST-ENGINE-DESIGN.md §1 rule 2)."""

import pathlib
import re

ENGINE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "app/ai/voice/agents/breeze_buddy/assist/engine"
)
FORBIDDEN = re.compile(r"shopify|woocommerce|magento|bigcommerce", re.IGNORECASE)


def test_engine_has_no_platform_branches():
    offenders = []
    for path in ENGINE.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(ENGINE)}:{number}: {line.strip()}")
    assert not offenders, "platform names inside engine/:\n" + "\n".join(offenders)
