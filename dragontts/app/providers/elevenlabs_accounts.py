"""ElevenLabs multi-account rotation for the Text-to-Dialogue sockets.

ELEVENLABS_ACCOUNT_ROTATE=false (the default): nothing here is used — the
residency key serves every request exactly as before.

ELEVENLABS_ACCOUNT_ROTATE=true: Text-to-Dialogue (eleven_v3* / eleven_v4*)
sockets are opened on the accounts listed in ELEVENLABS_ACCOUNTS_CONFIG, a
JSON list of::

    {"name": "global", "api_key_env": "ELEVENLABS_GLOBAL_API_KEY",
     "base_url": "https://api.elevenlabs.io", "max_websockets": 40,
     "traffic_percent": 20}

- ``api_key_env``: the NAME of the environment variable holding the account's
  key (the k8s Secret; dragontts/.env locally) — the config itself never
  carries a key, so it is safe in git and has a default. A bare ``api_key``
  is rejected.

- ``traffic_percent``: share of TTD cache misses sent to this account. The
  split is STRICT — a miss picked for an account only ever uses that
  account's sockets and waits for one of them when they're all busy; it never
  spills onto another account. The percentages must add up to 100.
- ``max_websockets``: the most TTD sockets the POD may hold on this account,
  across every pool (voice / model / language / rate). Each uvicorn worker
  keeps its own count, so a worker gets ``max_websockets // ELEVENLABS_WORKERS``.
- ``base_url``: https only (the key rides the WebSocket handshake) — global or
  India residency, per account.

The classic text-to-speech socket and the HTTP path keep the residency key
either way. An invalid config logs an error and leaves rotation OFF (today's
behavior) rather than failing startup: on a single-pod deploy a crash is an
outage, while the fallback is the setup prod already runs on.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from dotenv import dotenv_values

from app.core.config import settings
from app.core.logging import logger

if TYPE_CHECKING:
    from app.providers.elevenlabs_pool import ElevenLabsStreamPool


class AccountConfigError(ValueError):
    """ELEVENLABS_ACCOUNTS_CONFIG is malformed. The message never contains a key."""


@dataclass(frozen=True)
class ElevenLabsAccount:
    name: str
    # repr=False: the key never reaches a log line or a traceback.
    api_key: str = field(repr=False)
    key_env: str
    base_url: str
    max_websockets: int
    traffic_percent: float


# What an environment variable name looks like: UPPERCASE letters, digits and
# _ (max 64). ElevenLabs keys ("sk_" + lowercase hex) never match, so a key
# pasted into api_key_env is rejected here — WITHOUT being echoed — and can't
# reach the "is not set" message below, which does print the name.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def parse_accounts(
    raw: str, workers: int, env: Mapping[str, str] | None = None
) -> list[ElevenLabsAccount]:
    """Parse and validate ELEVENLABS_ACCOUNTS_CONFIG, resolving each account's
    key from the environment variable it names (``env``; default: the process
    environment). Raises AccountConfigError."""
    if env is None:
        env = os.environ
    try:
        data = json.loads(raw)
    except ValueError as e:
        # Not the JSON error text: it can quote part of the input (a key).
        raise AccountConfigError(
            f"not valid JSON (line {getattr(e, 'lineno', '?')})"
        ) from None
    if not isinstance(data, list) or not data:
        raise AccountConfigError("must be a non-empty JSON list of accounts")
    accounts: list[ElevenLabsAccount] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise AccountConfigError(f"account #{i + 1} is not an object")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AccountConfigError(f"account #{i + 1}: missing name")
        name = name.strip()
        if "api_key" in item:
            raise AccountConfigError(
                f"account {name}: a bare api_key is not allowed — put the key in "
                "an environment variable and name it in api_key_env"
            )
        key_env = item.get("api_key_env")
        if not isinstance(key_env, str) or not key_env.strip():
            raise AccountConfigError(f"account {name}: missing api_key_env")
        key_env = key_env.strip()
        if not _ENV_NAME.match(key_env):
            raise AccountConfigError(
                f"account {name}: api_key_env must be an environment variable "
                "NAME (UPPERCASE letters, digits, _), not a key"
            )
        api_key = (env.get(key_env) or "").strip()
        if not api_key:
            raise AccountConfigError(
                f"account {name}: environment variable {key_env} is not set"
            )
        base_url = item.get("base_url")
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise AccountConfigError(
                f"account {name}: base_url must start with https:// (the API key "
                "is sent in the WebSocket handshake)"
            )
        max_ws = item.get("max_websockets")
        if isinstance(max_ws, bool) or not isinstance(max_ws, int) or max_ws < 1:
            raise AccountConfigError(
                f"account {name}: max_websockets must be a whole number >= 1"
            )
        if max_ws // workers < 1:
            raise AccountConfigError(
                f"account {name}: max_websockets={max_ws} is below "
                f"ELEVENLABS_WORKERS={workers} — each worker's share would be 0"
            )
        pct = item.get("traffic_percent")
        if (
            isinstance(pct, bool)
            or not isinstance(pct, (int, float))
            or not math.isfinite(pct)  # json.loads accepts NaN / Infinity
            or pct < 0
        ):
            raise AccountConfigError(
                f"account {name}: traffic_percent must be a number >= 0"
            )
        accounts.append(
            ElevenLabsAccount(
                name=name,
                api_key=api_key,
                key_env=key_env,
                base_url=base_url.rstrip("/"),
                max_websockets=max_ws,
                traffic_percent=float(pct),
            )
        )
    names = [a.name for a in accounts]
    if len(set(names)) != len(names):
        raise AccountConfigError("account names must be unique")
    total = sum(a.traffic_percent for a in accounts)
    if abs(total - 100.0) > 1e-6:
        raise AccountConfigError(f"traffic_percent adds up to {total:g}, not 100")
    return accounts


class AccountBudget:
    """The TTD socket budget of ONE worker process, per account, shared by
    every TTD pool in that worker.

    ``share`` is the account's per-worker cap; ``open`` counts the sockets this
    worker holds on it right now (connecting, live, or reconnecting — a socket
    keeps its slot until it is closed for good).
    """

    def __init__(
        self,
        accounts: list[ElevenLabsAccount],
        workers: int,
        *,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.accounts = accounts
        self.share = {a.name: a.max_websockets // workers for a in accounts}
        self.open = {a.name: 0 for a in accounts}
        self._rng = rng
        self._pools: list[ElevenLabsStreamPool] = []

    def pick(self) -> ElevenLabsAccount:
        """The account for one miss, weighted by traffic_percent."""
        r = self._rng() * 100.0
        cumulative = 0.0
        for account in self.accounts:
            cumulative += account.traffic_percent
            if r < cumulative:
                return account
        # Float rounding at the top end: the last account with traffic.
        return next(a for a in reversed(self.accounts) if a.traffic_percent > 0)

    def take(self, name: str) -> bool:
        """Claim one socket slot on ``name``; False when its share is used up."""
        if self.open[name] >= self.share[name]:
            return False
        self.open[name] += 1
        return True

    def give(self, name: str) -> None:
        self.open[name] = max(0, self.open[name] - 1)

    def register(self, pool: ElevenLabsStreamPool) -> None:
        self._pools.append(pool)

    def unregister(self, pool: ElevenLabsStreamPool) -> None:
        if pool in self._pools:
            self._pools.remove(pool)

    def reclaim(self, name: str, requester: ElevenLabsStreamPool) -> bool:
        """Free one slot on ``name`` by closing an idle socket another pool
        holds on it. Without this, sockets that a pool opened in a burst and
        no longer uses would pin the account's share forever, and a different
        voice could never get a socket on that account. Only a live socket
        with nothing in flight that has sat unused for a while qualifies — a
        connecting one never does, so two pools can't keep stealing each
        other's fresh sockets."""
        for pool in self._pools:
            if pool is not requester and pool._release_idle_socket(name):
                return True
        return False

    def summary(self) -> str:
        return ", ".join(f"{n} {self.open[n]}/{self.share[n]}" for n in self.share)


def _environment() -> dict[str, str]:
    """Where api_key_env names are looked up: the process environment (the
    k8s Secret in prod), over dragontts/.env (local runs — settings read .env
    themselves, but its values never reach os.environ)."""
    merged = {k: v for k, v in dotenv_values(".env").items() if v is not None}
    merged.update(os.environ)
    return merged


def load_account_budget() -> AccountBudget | None:
    """The worker's budget when rotation is ON and the config is valid; None
    (rotation off, today's behavior) otherwise."""
    if not settings.elevenlabs_account_rotate:
        return None
    # Clamped, not validated at startup: a bad value must not crash a pod
    # that may not even use rotation.
    workers = max(1, settings.elevenlabs_workers)
    try:
        accounts = parse_accounts(
            settings.elevenlabs_accounts_config, workers, _environment()
        )
    except AccountConfigError as e:
        logger.error(
            f"ELEVENLABS_ACCOUNT_ROTATE=true but ELEVENLABS_ACCOUNTS_CONFIG is "
            f"invalid ({e}) — rotation is OFF: Text-to-Dialogue stays on the "
            f"residency key exactly as before"
        )
        return None
    budget = AccountBudget(accounts, workers)
    logger.info(
        "ElevenLabs account rotation ON (Text-to-Dialogue only): "
        + "; ".join(
            f"{a.name} {a.traffic_percent:g}% of misses, {a.max_websockets} "
            f"sockets/pod = {budget.share[a.name]}/worker, {a.base_url}, "
            f"key from ${a.key_env}"
            for a in accounts
        )
    )
    return budget
