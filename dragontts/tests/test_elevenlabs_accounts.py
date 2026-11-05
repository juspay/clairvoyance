"""ElevenLabs multi-account rotation for the Text-to-Dialogue sockets.

ELEVENLABS_ACCOUNT_ROTATE=false must leave everything exactly as it was; when
true, TTD misses split STRICTLY by traffic_percent across the accounts in
ELEVENLABS_ACCOUNTS_CONFIG, each account's sockets open on its own key and
host, and no worker ever holds more than its share of an account's
max_websockets. The sockets are fakes — no network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time

import pytest
from loguru import logger

from app.core.config import settings
from app.providers import elevenlabs_pool
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.elevenlabs_accounts import (
    AccountBudget,
    AccountConfigError,
    load_account_budget,
    parse_accounts,
)
from app.providers.elevenlabs_pool import ElevenLabsStreamPool, SocketUnavailable
from tests.test_elevenlabs_v3 import BASE, V3_MODEL, VOICE, FakeSocket

KEY_A = "sk_test_account_a_secret"
KEY_B = "sk_test_account_b_secret"
HOST_A = "https://api.in.residency.elevenlabs.io"
HOST_B = "https://api.elevenlabs.io"
VOICE_2 = "xkyzgonjVuaQAEkkh0LV"
ENV = {"TEST_EL_KEY_A": KEY_A, "TEST_EL_KEY_B": KEY_B}


@pytest.fixture(autouse=True)
def _account_keys(monkeypatch):
    """The keys live in env vars; the config only names them."""
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


def _config(a_pct=80, b_pct=20, a_max=8, b_max=8, **overrides) -> list[dict]:
    accounts = [
        {
            "name": "india",
            "api_key_env": "TEST_EL_KEY_A",
            "base_url": HOST_A,
            "max_websockets": a_max,
            "traffic_percent": a_pct,
        },
        {
            "name": "global",
            "api_key_env": "TEST_EL_KEY_B",
            "base_url": HOST_B + "/",
            "max_websockets": b_max,
            "traffic_percent": b_pct,
        },
    ]
    for key, value in overrides.items():
        accounts[0][key] = value
    return accounts


class RecordingConnect:
    """ConnectFn double recording the URI + headers of every socket; sockets
    on a host listed in ``fail_hosts`` never connect."""

    def __init__(self, fail_hosts: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.sockets: list[FakeSocket] = []
        self.fail_hosts = fail_hosts

    def __call__(self, uri: str, headers: dict):
        self.calls.append((uri, headers))
        outer = self

        class _Ctx:
            async def __aenter__(self):
                if any(uri.startswith(h) for h in outer.fail_hosts):
                    raise OSError("handshake refused")
                socket = FakeSocket()
                outer.sockets.append(socket)
                return socket

            async def __aexit__(self, *args):
                return False

        return _Ctx()


def _budget(rng=None, **kw) -> AccountBudget:
    accounts = parse_accounts(json.dumps(_config(**kw)), workers=4, env=ENV)
    return AccountBudget(accounts, 4, rng=rng or random.random)


def _rotated_pool(budget, connect, voice=VOICE, **kw) -> ElevenLabsStreamPool:
    return ElevenLabsStreamPool(
        api_key="",
        voice_id=voice,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=connect,
        accounts=budget,
        acquire_timeout=kw.pop("acquire_timeout", 1.0),
        **kw,
    )


@pytest.fixture
def logs():
    captured: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: captured.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    yield captured
    logger.remove(sink)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_parses_a_valid_config():
    accounts = parse_accounts(json.dumps(_config()), workers=4, env=ENV)
    assert [a.name for a in accounts] == ["india", "global"]
    assert [a.api_key for a in accounts] == [KEY_A, KEY_B], "resolved from env"
    assert accounts[1].base_url == HOST_B, "trailing slash stripped"
    assert [a.traffic_percent for a in accounts] == [80.0, 20.0]
    assert KEY_A not in repr(accounts), "the key never shows in a repr"


@pytest.mark.parametrize(
    "raw, needle",
    [
        ('[{"name": "x", "api_key": "' + KEY_A + '",', "not valid JSON"),
        ("{}", "non-empty JSON list"),
        ("[]", "non-empty JSON list"),
        (json.dumps(_config(name="")), "missing name"),
        (json.dumps(_config(api_key_env=" ")), "missing api_key_env"),
        (json.dumps(_config(api_key=KEY_A)), "bare api_key is not allowed"),
        (json.dumps(_config(api_key_env=KEY_A + "-x")), "NAME"),
        (json.dumps(_config(api_key_env="NOT_SET_ANYWHERE")), "is not set"),
        (json.dumps(_config(base_url="http://api.elevenlabs.io")), "https://"),
        (json.dumps(_config(max_websockets=0)), "max_websockets"),
        (json.dumps(_config(max_websockets=True)), "max_websockets"),
        (json.dumps(_config(max_websockets=3)), "share would be 0"),
        (json.dumps(_config(traffic_percent=-5)), "traffic_percent"),
        (json.dumps(_config(a_pct=70)), "adds up to 90"),
        (json.dumps(_config(name="global")), "unique"),
    ],
)
def test_rejects_an_invalid_config_without_leaking_the_key(raw, needle):
    with pytest.raises(AccountConfigError) as err:
        parse_accounts(raw, workers=4, env=ENV)
    assert needle in str(err.value)
    assert KEY_A not in str(err.value) and KEY_B not in str(err.value)


def test_rotation_off_ignores_the_config(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", False)
    monkeypatch.setattr(settings, "elevenlabs_accounts_config", json.dumps(_config()))
    assert load_account_budget() is None


def test_invalid_config_leaves_rotation_off_and_says_so(monkeypatch, logs):
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", True)
    monkeypatch.setattr(
        settings, "elevenlabs_accounts_config", json.dumps(_config(a_pct=1))
    )
    assert load_account_budget() is None
    errors = [m for level, m in logs if level == "ERROR"]
    assert errors and "rotation is OFF" in errors[0]
    assert all(KEY_A not in m and KEY_B not in m for _, m in logs)


def test_valid_config_turns_rotation_on_without_logging_keys(monkeypatch, logs):
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", True)
    monkeypatch.setattr(settings, "elevenlabs_workers", 4)
    monkeypatch.setattr(
        settings,
        "elevenlabs_accounts_config",
        json.dumps(_config(a_max=100, b_max=40)),
    )
    budget = load_account_budget()
    assert budget is not None
    assert budget.share == {"india": 25, "global": 10}, "max_websockets // workers"
    assert any("rotation ON" in m for _, m in logs)
    assert all(KEY_A not in m and KEY_B not in m for _, m in logs)


# ---------------------------------------------------------------------------
# Traffic split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "r, expected",
    [(0.0, "india"), (0.7999, "india"), (0.8, "global"), (0.9999, "global")],
)
def test_split_boundaries(r, expected):
    assert _budget(rng=lambda: r).pick().name == expected


def test_a_zero_percent_account_is_never_picked():
    budget = _budget(a_pct=0, b_pct=100)
    assert {budget.pick().name for _ in range(2000)} == {"global"}


def test_split_follows_the_percentages():
    rng = random.Random(7)
    budget = _budget(rng=rng.random)
    picks = [budget.pick().name for _ in range(20000)]
    share = picks.count("india") / len(picks)
    assert 0.78 < share < 0.82


# ---------------------------------------------------------------------------
# Pool behaviour under rotation
# ---------------------------------------------------------------------------


async def test_each_account_connects_with_its_own_key_and_host():
    picks = iter([0.1, 0.9])  # india, then global
    budget = _budget(rng=lambda: next(picks))
    connect = RecordingConnect()
    pool = _rotated_pool(budget, connect)
    try:
        a = await pool.acquire()
        b = await pool.acquire()
        assert (a.account, b.account) == ("india", "global")
        (uri_a, hdr_a), (uri_b, hdr_b) = connect.calls
        assert uri_a.startswith(
            "wss://api.in.residency.elevenlabs.io/v1/text-to-dialogue/"
        )
        assert uri_b.startswith("wss://api.elevenlabs.io/v1/text-to-dialogue/")
        assert uri_a.split("?", 1)[1] == uri_b.split("?", 1)[1], "same query"
        assert hdr_a == {"xi-api-key": KEY_A}
        assert hdr_b == {"xi-api-key": KEY_B}
    finally:
        await pool.aclose()


async def test_split_is_strict_a_full_account_waits_even_if_the_other_is_free():
    pick = {"r": 0.0}
    budget = _budget(rng=lambda: pick["r"], a_max=4, b_max=4)  # 1 socket each
    connect = RecordingConnect()
    pool = _rotated_pool(budget, connect, acquire_timeout=0.3)
    try:
        india = await pool.acquire()
        india.inflight -= 1  # india: a live socket with 4 free slots
        pick["r"] = 0.99
        held = [await pool.acquire() for _ in range(4)]  # global's 4 slots
        assert {c.account for c in held} == {"global"}
        with pytest.raises(SocketUnavailable, match="account global"):
            await pool.acquire()
        assert india.inflight == 0, "the free india socket was not used"
        assert budget.open == {"india": 1, "global": 1}
    finally:
        await pool.aclose()


async def test_a_burst_opens_only_the_sockets_it_needs():
    budget = _budget(rng=lambda: 0.0, a_max=40)  # india, 10 per worker
    connect = RecordingConnect()
    pool = _rotated_pool(budget, connect)
    try:
        held = await asyncio.gather(*(pool.acquire() for _ in range(8)))
        assert len(connect.calls) == 2, "8 requests / 4 slots = 2 sockets, not 8"
        assert sorted(c.inflight for c in pool._conns) == [4, 4]
        for c in held:
            c.inflight -= 1
    finally:
        await pool.aclose()


async def test_an_accounts_share_is_never_exceeded_across_pools():
    budget = _budget(rng=lambda: 0.0, a_max=8)  # india, 2 per worker
    connect = RecordingConnect()
    p1 = _rotated_pool(budget, connect, acquire_timeout=0.3)
    p2 = _rotated_pool(budget, connect, language="en", acquire_timeout=0.3)
    try:
        held = await asyncio.gather(*(p1.acquire() for _ in range(8)))
        assert budget.open["india"] == 2
        # Every india socket is busy: the other pool waits, opens nothing.
        with pytest.raises(SocketUnavailable):
            await p2.acquire()
        assert len(connect.calls) == 2 and p2._conns == []
        for c in held:
            c.inflight -= 1
    finally:
        await p1.aclose()
        await p2.aclose()


async def test_an_idle_socket_in_another_pool_is_reused():
    budget = _budget(rng=lambda: 0.0, a_max=4)  # india, 1 per worker
    connect = RecordingConnect()
    p1 = _rotated_pool(budget, connect)
    p2 = _rotated_pool(budget, connect, language="en")
    try:
        conn = await p1.acquire()
        conn.inflight -= 1
        conn.last_used = time.monotonic() - 60  # idle for a minute
        got = await p2.acquire()
        assert got.account == "india"
        assert p1._conns == [] and p2._conns == [got]
        assert budget.open["india"] == 1, "the slot moved, it wasn't added"
        await asyncio.sleep(0.05)
        assert connect.sockets[0].closed, "the idle socket was closed"
        got.inflight -= 1
    finally:
        await p1.aclose()
        await p2.aclose()


@pytest.mark.parametrize("busy", [True, False])
async def test_busy_or_just_used_sockets_are_not_taken(busy):
    budget = _budget(rng=lambda: 0.0, a_max=4)
    connect = RecordingConnect()
    p1 = _rotated_pool(budget, connect)
    p2 = _rotated_pool(budget, connect, language="en", acquire_timeout=0.3)
    try:
        conn = await p1.acquire()
        conn.inflight -= 1
        conn.last_used = time.monotonic() - 60  # long idle...
        if busy:
            conn.inflight += 1  # ...but a sentence is on it right now
        else:
            # ...until a real sentence just finished on it (stream() must
            # stamp last_used when it releases the slot).
            async def speak():
                async for _ in p1.stream({"text": "hi"}):
                    pass

            task = asyncio.create_task(speak())
            socket = connect.sockets[0]
            await asyncio.wait_for(_flushed(socket), 2.0)
            ctx = next(m["context_id"] for m in socket.sent if m.get("flush"))
            socket.feed({"context_id": ctx, "audio": base64.b64encode(b"ab").decode()})
            socket.feed({"context_id": ctx, "is_final_audio_for_turn": True})
            await asyncio.wait_for(task, 2.0)
        with pytest.raises(SocketUnavailable):
            await p2.acquire()
        assert p1._conns == [conn] and not connect.sockets[0].closed
    finally:
        await p1.aclose()
        await p2.aclose()


async def _flushed(socket) -> None:
    while not any(m.get("flush") for m in socket.sent):
        await asyncio.sleep(0.01)


def test_rejects_nan_and_infinite_percentages():
    for bad in ("NaN", "Infinity"):
        raw = json.dumps(_config()).replace(
            '"traffic_percent": 80', f'"traffic_percent": {bad}'
        )
        with pytest.raises(AccountConfigError, match="traffic_percent"):
            parse_accounts(raw, workers=4, env=ENV)


async def test_start_does_nothing_under_rotation():
    connect = RecordingConnect()
    pool = _rotated_pool(_budget(), connect, min_size=2)
    try:
        await pool.start()
        await asyncio.sleep(0.05)
        assert pool._conns == [] and connect.calls == []
    finally:
        await pool.aclose()


def test_bad_worker_count_is_clamped_not_fatal(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", True)
    monkeypatch.setattr(settings, "elevenlabs_workers", 0)
    monkeypatch.setattr(settings, "elevenlabs_accounts_config", json.dumps(_config()))
    budget = load_account_budget()
    assert budget is not None and budget.share == {"india": 8, "global": 8}


async def test_a_broken_account_only_fails_its_own_share():
    picks = iter([0.9, 0.9, 0.9, 0.1])  # global x3 (broken), then india
    budget = _budget(rng=lambda: next(picks))
    connect = RecordingConnect(fail_hosts=("wss://api.elevenlabs.io",))
    pool = _rotated_pool(budget, connect, acquire_timeout=0.2, failure_threshold=2)
    try:
        for _ in range(2):
            with pytest.raises(SocketUnavailable, match="account global"):
                await pool.acquire()
        # global's circuit is open: fails fast, without another wait.
        t0 = time.monotonic()
        with pytest.raises(SocketUnavailable, match="circuit open"):
            await pool.acquire()
        assert time.monotonic() - t0 < 0.1
        conn = await pool.acquire()  # india is unaffected
        assert conn.account == "india"
        conn.inflight -= 1
    finally:
        await pool.aclose()


async def test_closing_a_pool_returns_its_slots():
    budget = _budget(rng=lambda: 0.0)
    pool = _rotated_pool(budget, RecordingConnect())
    conn = await pool.acquire()
    conn.inflight -= 1
    assert budget.open["india"] == 1
    await pool.aclose()
    assert budget.open["india"] == 0
    assert pool not in budget._pools


# ---------------------------------------------------------------------------
# Provider wiring
# ---------------------------------------------------------------------------


async def test_rotation_off_builds_todays_pools(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", False)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        pool = provider._get_pool(VOICE, V3_MODEL, False, "hi", 8000)
        assert pool is not None and pool._accounts is None
        assert pool._headers == {"xi-api-key": "k"}
    finally:
        await provider.aclose()


async def test_rotation_on_applies_to_dialogue_pools_only(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", True)
    monkeypatch.setattr(settings, "elevenlabs_workers", 4)
    monkeypatch.setattr(settings, "elevenlabs_accounts_config", json.dumps(_config()))
    # The account budgets size TTD pools, not ELEVENLABS_DIALOGUE_POOL_SIZE.
    monkeypatch.setattr(settings, "elevenlabs_dialogue_pool_size", 0)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        ttd = provider._get_pool(VOICE, "eleven_v4_turbo", False, "hi", 8000)
        assert ttd is not None and ttd._accounts is provider._accounts
        classic = provider._get_pool(VOICE, "eleven_flash_v2_5", language="en")
        assert classic is not None and classic._accounts is None
        assert classic._headers == {
            "xi-api-key": "k"
        }, "classic keeps the residency key"
    finally:
        await provider.aclose()


async def test_rotated_sockets_carry_utterances_end_to_end(monkeypatch):
    """A full sentence through the provider on the account the split picked."""
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", True)
    monkeypatch.setattr(settings, "elevenlabs_workers", 4)
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 8000)
    monkeypatch.setattr(
        settings,
        "elevenlabs_accounts_config",
        json.dumps(_config(a_pct=0, b_pct=100)),
    )
    connect = RecordingConnect()
    monkeypatch.setattr(
        elevenlabs_pool,
        "connect",
        lambda uri, additional_headers=None, open_timeout=None: connect(
            uri, additional_headers
        ),
    )
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        task = asyncio.create_task(
            provider.synth(
                text="नमस्ते", voice_id=VOICE, model=V3_MODEL, language="hi", params={}
            )
        )

        async def flushed():
            while not (
                connect.sockets and any(m.get("flush") for m in connect.sockets[0].sent)
            ):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(flushed(), 2.0)
        socket = connect.sockets[0]
        ctx = next(m["context_id"] for m in socket.sent if m.get("flush"))

        socket.feed(
            {"context_id": ctx, "audio": base64.b64encode(b"\x01\x02" * 40).decode()}
        )
        socket.feed({"context_id": ctx, "is_final_audio_for_turn": True})
        result = await asyncio.wait_for(task, 2.0)
        assert result.audio == b"\x01\x02" * 40
        assert connect.calls[0][1] == {"xi-api-key": KEY_B}
        assert connect.calls[0][0].startswith("wss://api.elevenlabs.io/")
    finally:
        await provider.aclose()


async def test_rotated_dialogue_pools_are_shared_by_every_voice(monkeypatch):
    """Under rotation one TTD pool per (model, language, rate) serves all
    voices; each sentence registers its own voice on the shared socket."""
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", True)
    monkeypatch.setattr(settings, "elevenlabs_workers", 4)
    monkeypatch.setattr(
        settings,
        "elevenlabs_accounts_config",
        json.dumps(_config(a_pct=100, b_pct=0)),
    )
    connect = RecordingConnect()
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        found = provider._get_pool(VOICE, V3_MODEL, False, "hi", 8000)
        assert found is not None
        pool: ElevenLabsStreamPool = found
        assert pool is provider._get_pool(VOICE_2, V3_MODEL, False, "hi", 8000)
        assert pool is not provider._get_pool(VOICE, V3_MODEL, False, "en", 8000)
        pool._connect_fn = connect

        async def speak(voice):
            async for _ in pool.stream({"text": "hi", "voice_id": voice}):
                pass

        tasks = [asyncio.create_task(speak(v)) for v in (VOICE, VOICE_2)]

        async def both_flushed():
            while not (
                connect.sockets
                and sum(1 for m in connect.sockets[0].sent if m.get("flush")) == 2
            ):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(both_flushed(), 2.0)
        assert len(connect.sockets) == 1, "two voices, one socket"
        sent = connect.sockets[0].sent
        voice_of = {
            m["context_id"]: m["voices"][0]
            for m in sent
            if "voices" in m and m["context_id"] != "dragontts-keepalive"
        }
        assert sorted(voice_of.values()) == sorted([VOICE, VOICE_2])
        inputs = [(m["context_id"], i) for m in sent for i in m.get("inputs", [])]
        assert len(inputs) == 2
        assert all(i["voice_id"] == voice_of[ctx] for ctx, i in inputs)
        for ctx in voice_of:
            connect.sockets[0].feed(
                {"context_id": ctx, "audio": base64.b64encode(b"ab").decode()}
            )
            connect.sockets[0].feed(
                {"context_id": ctx, "is_final_audio_for_turn": True}
            )
        for task in tasks:
            await asyncio.wait_for(task, 2.0)
    finally:
        await provider.aclose()


async def test_rotation_off_keeps_one_pool_per_voice(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", False)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        a = provider._get_pool(VOICE, V3_MODEL, False, "hi", 8000)
        b = provider._get_pool(VOICE_2, V3_MODEL, False, "hi", 8000)
        assert a is not None and b is not None and a is not b
        assert (a._voice_id, b._voice_id) == (VOICE, VOICE_2)
    finally:
        await provider.aclose()


def test_the_default_config_is_valid_and_holds_no_key():
    from app.core.config import Settings

    default = Settings.model_fields["elevenlabs_accounts_config"].default
    assert "sk_" not in default and '"api_key"' not in default
    env = {
        "ELEVENLABS_INDIAN_RESIDENCY_API_KEY": KEY_A,
        "ELEVENLABS_GLOBAL_API_KEY": KEY_B,
    }
    accounts = parse_accounts(default, workers=4, env=env)
    assert [(a.name, a.key_env, a.traffic_percent) for a in accounts] == [
        ("india", "ELEVENLABS_INDIAN_RESIDENCY_API_KEY", 80.0),
        ("global", "ELEVENLABS_GLOBAL_API_KEY", 20.0),
    ]
    assert [a.api_key for a in accounts] == [KEY_A, KEY_B]


def test_a_missing_key_env_var_leaves_rotation_off(monkeypatch, logs):
    monkeypatch.delenv("TEST_EL_KEY_B")
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", True)
    monkeypatch.setattr(settings, "elevenlabs_accounts_config", json.dumps(_config()))
    monkeypatch.chdir("/tmp")  # no .env to fall back on
    assert load_account_budget() is None
    assert any("TEST_EL_KEY_B is not set" in m for level, m in logs if level == "ERROR")


def test_keys_can_come_from_the_dotenv_file(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_EL_KEY_B")
    (tmp_path / ".env").write_text("TEST_EL_KEY_B=" + KEY_B + "\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", True)
    monkeypatch.setattr(settings, "elevenlabs_accounts_config", json.dumps(_config()))
    budget = load_account_budget()
    assert budget is not None
    assert budget.accounts[1].api_key == KEY_B


@pytest.mark.parametrize("rotated", [True, False])
async def test_a_turn_with_no_audio(rotated):
    """Rotation: an account answering with is_final and no audio is an error
    (retried, never cached as silence). Single-key pools: unchanged."""
    connect = RecordingConnect()
    if rotated:
        pool = _rotated_pool(_budget(rng=lambda: 0.9), connect)
    else:
        pool = ElevenLabsStreamPool(
            api_key="k",
            voice_id=VOICE,
            model_id=V3_MODEL,
            base_url=BASE,
            connect_fn=connect,
            min_size=1,
            max_size=1,
        )
        await pool.start()
    try:
        got: list[bytes] = []

        async def speak():
            async for chunk in pool.stream({"text": "hi"}):
                got.append(chunk)

        task = asyncio.create_task(speak())
        await asyncio.wait_for(_until_socket(connect), 2.0)
        socket = connect.sockets[0]
        await asyncio.wait_for(_flushed(socket), 2.0)
        ctx = next(m["context_id"] for m in socket.sent if m.get("flush"))
        socket.feed({"context_id": ctx, "is_final": True})
        if rotated:
            with pytest.raises(
                elevenlabs_pool.ProviderError,
                match="global ended the turn with no audio",
            ):
                await asyncio.wait_for(task, 2.0)
        else:
            await asyncio.wait_for(task, 2.0)
            assert got == []
    finally:
        await pool.aclose()


async def _until_socket(connect) -> None:
    while not connect.sockets:
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "pasted",
    [
        "sk_" + "0123456789abcdef" * 3,  # a real-shaped ElevenLabs key
        "SK_" + "0123456789ABCDEF" * 5,  # uppercase, but longer than any env name
        "abcdef0123456789abcdef0123456789",
    ],
)
def test_a_key_pasted_into_api_key_env_is_never_echoed(monkeypatch, pasted, logs):
    raw = json.dumps(_config(api_key_env=pasted))
    with pytest.raises(AccountConfigError) as err:
        parse_accounts(raw, workers=4, env=ENV)
    assert pasted not in str(err.value) and "NAME" in str(err.value)
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", True)
    monkeypatch.setattr(settings, "elevenlabs_accounts_config", raw)
    assert load_account_budget() is None
    assert all(pasted not in m for _, m in logs)
