"""Live ElevenLabs connection metrics, pod-wide (GET /stats/elevenlabs/live).

Each uvicorn worker owns its own sockets, so a request only ever sees the
worker it lands on. Every worker therefore publishes a small JSON snapshot of
its own state to a per-server directory under the temp dir, and the endpoint
adds them up. The cost is kept minimal on purpose:

- events (a socket connecting or going down, a sentence starting or ending on
  a socket, an HTTP synth call) are O(1) dict updates — no I/O, no locks;
- a worker writes its snapshot only after something changed, at most once per
  ``_PUBLISH_EVERY_SECS`` (one small file write); an idle worker writes nothing;
- the endpoint reads the files only when it is called.

Peaks are kept per minute for the last hour (windows 5m / 15m / 60m) plus
since the worker started. A worker's own peaks are exact. Pod-wide peaks are
SAMPLED: each worker adds up the pod's current values when it publishes, so a
spike shorter than about a second can be missed (sockets change slowly, so
their pod peak is close to exact; per-sentence in-flight counts less so).

Besides the ElevenLabs sockets, the same snapshot carries pod-level gauges
(incoming /tts/bytes + /tts/stream requests being served; sentences in flight
to ElevenLabs over either path) and RATES — incoming requests, cache hits /
misses, sentences sent to ElevenLabs — counted per minute (exact pod-wide over
the last hour: the workers' per-minute counts are summed) and per second
(pod-wide peak sampled from each worker's last _RECENT_SECS seconds).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from app.core.logging import logger

# Gauges (current values, tracked per (model, account)).
SOCKETS_OPEN = "sockets_open"  # connected sockets
WS_IN_FLIGHT = "websocket_in_flight"  # sentences being spoken on a socket
HTTP_IN_FLIGHT = "http_in_flight"  # one-shot HTTP synth calls in progress
GAUGES = (SOCKETS_OPEN, WS_IN_FLIGHT, HTTP_IN_FLIGHT)

# Counters (since the worker started).
SOCKETS_CONNECTED = "sockets_connected"
SOCKETS_DROPPED = "sockets_dropped"  # lost while in use, not closed by us
CONNECT_FAILURES = "connect_failures"
SOCKETS_REFRESHED = "sockets_refreshed"
SOCKET_UNAVAILABLE = "socket_unavailable"  # no socket within acquire timeout
FIRST_AUDIO_TIMEOUTS = "first_audio_timeouts"
HTTP_REQUESTS = "http_requests"
COUNTERS = (
    SOCKETS_CONNECTED,
    SOCKETS_DROPPED,
    CONNECT_FAILURES,
    SOCKETS_REFRESHED,
    SOCKET_UNAVAILABLE,
    FIRST_AUDIO_TIMEOUTS,
    HTTP_REQUESTS,
)

# Pod-level gauges (no model / account).
REQUESTS_IN_FLIGHT = "requests_in_flight"  # /tts/bytes + /tts/stream being served
ELEVENLABS_IN_FLIGHT = "elevenlabs_in_flight"  # websocket + http, together
POD_GAUGES = (REQUESTS_IN_FLIGHT, ELEVENLABS_IN_FLIGHT)
ALL_GAUGES = GAUGES + POD_GAUGES

# Rates: events counted per second and per minute.
REQUESTS = "requests"  # incoming /tts/bytes + /tts/stream requests
CACHE_HITS = "cache_hits"  # sentences served from the cache
CACHE_MISSES = "cache_misses"  # sentences synthesized by a provider
ELEVENLABS_REQUESTS = "elevenlabs_requests"  # sentences sent to ElevenLabs
ELEVENLABS_WEBSOCKET = "elevenlabs_websocket"  # ... over a socket
ELEVENLABS_HTTP = "elevenlabs_http"  # ... over one-shot HTTP
RATES = (
    REQUESTS,
    CACHE_HITS,
    CACHE_MISSES,
    ELEVENLABS_REQUESTS,
    ELEVENLABS_WEBSOCKET,
    ELEVENLABS_HTTP,
)

DEFAULT_ACCOUNT = "default"  # the single residency key (rotation off)
WINDOWS_MIN = (5, 15, 60)
_KEEP_MINUTES = 60
_RECENT_SECS = 15  # per-second counts kept for the pod-wide per-second peak
_PUBLISH_EVERY_SECS = 1.0


def _minute(now: float) -> int:
    return int(now // 60)


def _record(buckets: dict[int, int], minute: int, value: int) -> None:
    if value > buckets.get(minute, -1):
        buckets[minute] = value


def _prune(buckets: dict[int, int], minute: int) -> None:
    for m in [m for m in buckets if m <= minute - _KEEP_MINUTES]:
        del buckets[m]


def windows(buckets: dict[int, int], current: int, since_start: int, now: float):
    """Peak per window from per-minute maxima. ``current`` counts too: a value
    held without changes leaves no bucket, but it IS in every window."""
    minute = _minute(now)
    out: dict[str, int] = {}
    for w in WINDOWS_MIN:
        recent = [v for m, v in buckets.items() if m > minute - w]
        out[f"{w}m"] = max([current, *recent])
    out["since_start"] = max(current, since_start)
    return out


class _Peak:
    """Per-minute maxima for the last hour + the all-time maximum."""

    __slots__ = ("buckets", "all")

    def __init__(self) -> None:
        self.buckets: dict[int, int] = {}
        self.all = 0

    def note(self, value: int, now: float) -> None:
        _record(self.buckets, _minute(now), value)
        if value > self.all:
            self.all = value

    def dump(self, now: float) -> dict:
        _prune(self.buckets, _minute(now))
        return {
            "buckets": {str(m): v for m, v in self.buckets.items()},
            "all": self.all,
        }


class _Rate:
    """Events per minute (last hour, exact) and per second (this worker's
    peak, exact; the last _RECENT_SECS seconds kept for the pod-wide peak)."""

    __slots__ = ("total", "minutes", "sec", "sec_count", "recent", "sec_peak")

    def __init__(self) -> None:
        self.total = 0
        self.minutes: dict[int, int] = {}
        self.sec = 0
        self.sec_count = 0
        self.recent: dict[int, int] = {}
        self.sec_peak = _Peak()

    def hit(self, now: float, n: int) -> None:
        self._roll(int(now))
        self.sec_count += n
        m = _minute(now)
        self.minutes[m] = self.minutes.get(m, 0) + n
        self.total += n

    def _roll(self, sec: int) -> None:
        if sec == self.sec:
            return
        if self.sec_count:
            self.recent[self.sec] = self.sec_count
            self.sec_peak.note(self.sec_count, self.sec)
        self.sec, self.sec_count = sec, 0

    def seconds(self, now: float) -> dict[int, int]:
        """Counts of the last _RECENT_SECS seconds, the current one included."""
        self._roll(int(now))
        for s in [s for s in self.recent if s <= int(now) - _RECENT_SECS]:
            del self.recent[s]
        out = dict(self.recent)
        if self.sec_count:
            out[self.sec] = self.sec_count
        return out

    def dump(self, now: float) -> dict:
        recent = self.seconds(now)
        _prune(self.minutes, _minute(now))
        peak = self.sec_peak.dump(now)
        if self.sec_count:  # the current second counts too
            key = str(_minute(self.sec))
            peak["buckets"][key] = max(peak["buckets"].get(key, 0), self.sec_count)
            peak["all"] = max(peak["all"], self.sec_count)
        return {
            "total": self.total,
            "minutes": {str(m): v for m, v in self.minutes.items()},
            "seconds": {str(s): v for s, v in recent.items()},
            "second_peak": peak,
        }


class LiveStats:
    """This worker's live ElevenLabs connection state (one per process)."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self._cur: dict[tuple[str, str, str], int] = {}
        self._total = dict.fromkeys(GAUGES, 0)
        self._by_account: dict[tuple[str, str], int] = {}
        self.counters = dict.fromkeys(COUNTERS, 0)
        self._rates = {r: _Rate() for r in RATES}
        # Exact worker peaks: key = gauge name, or "gauge@account".
        self._peaks: dict[str, _Peak] = {}
        # Sampled pod-wide totals (taken at each publish).
        self._pod_peaks: dict[str, _Peak] = {}
        self._pod_last: dict[str, int] = {}
        self._dir: Path | None = None
        self._pools: Callable[[], Iterable[Any]] = lambda: ()
        self._budget: Any = None
        self._pending: Any = None
        self._last_publish = 0.0

    # -- events (hot path: O(1), no I/O) ------------------------------------

    def add(self, gauge: str, model: str, account: str | None, delta: int) -> None:
        account = account or DEFAULT_ACCOUNT
        now = time.time()
        key = (gauge, model, account)
        self._cur[key] = self._cur.get(key, 0) + delta
        if not self._cur[key]:
            del self._cur[key]
        before = self._total.get(gauge, 0)
        self._total[gauge] = before + delta
        self._note_peak(gauge, before, delta, now)
        before = self._by_account.get((gauge, account), 0)
        self._by_account[(gauge, account)] = before + delta
        self._note_peak(f"{gauge}@{account}", before, delta, now)
        if gauge in (WS_IN_FLIGHT, HTTP_IN_FLIGHT):
            # Every sentence sent to ElevenLabs, either way, starts here.
            self.level(ELEVENLABS_IN_FLIGHT, delta)
            if delta > 0:
                self.event(ELEVENLABS_REQUESTS, delta)
                self.event(
                    ELEVENLABS_WEBSOCKET if gauge == WS_IN_FLIGHT else ELEVENLABS_HTTP,
                    delta,
                )
        self._schedule()

    def level(self, gauge: str, delta: int) -> None:
        """Change a pod-level gauge (no model / account): POD_GAUGES."""
        before = self._total.get(gauge, 0)
        self._total[gauge] = before + delta
        self._note_peak(gauge, before, delta, time.time())
        self._schedule()

    def event(self, rate: str, n: int = 1) -> None:
        """Count ``n`` events of ``rate`` (one of RATES) now."""
        self._rates[rate].hit(time.time(), n)
        self._schedule()

    def _note_peak(self, key: str, before: int, delta: int, now: float) -> None:
        peak = self._peaks.get(key)
        if peak is None:
            peak = self._peaks[key] = _Peak()
        # The value before the change was held into this minute too.
        peak.note(max(before, before + delta), now)

    def current(self, gauge: str, account: str | None = None) -> int:
        """This worker's current value of ``gauge`` (optionally one account)."""
        if account is None:
            return self._total.get(gauge, 0)
        return self._by_account.get((gauge, account), 0)

    def count(self, counter: str) -> None:
        self.counters[counter] = self.counters.get(counter, 0) + 1
        self._schedule()

    # -- publishing ----------------------------------------------------------

    def start(
        self,
        pools: Callable[[], Iterable[Any]],
        budget: Any = None,
        directory: Path | None = None,
    ) -> None:
        """Begin publishing (lifespan startup). ``pools`` lists the provider's
        socket pools; ``budget`` is the rotation budget (None = off)."""
        self._pools = pools
        self._budget = budget
        self._dir = directory or default_dir()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(
                f"ElevenLabs live metrics off — can't create {self._dir}: {e}"
            )
            self._dir = None
            return
        self.publish()

    def stop(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        if self._dir is not None:
            try:
                self._file(os.getpid()).unlink(missing_ok=True)
                self._dir.rmdir()  # the last worker out removes the directory
            except OSError:
                pass  # other workers' files are still there
        self._dir = None

    def _file(self, pid: int) -> Path:
        assert self._dir is not None
        return self._dir / f"worker-{pid}.json"

    def _schedule(self) -> None:
        """Publish soon — at most once per _PUBLISH_EVERY_SECS, only after a
        change, never more than one pending."""
        if self._dir is None or self._pending is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(0.0, self._last_publish + _PUBLISH_EVERY_SECS - time.monotonic())
        self._pending = loop.call_later(delay, self.publish)

    def publish(self) -> None:
        self._pending = None
        self._last_publish = time.monotonic()
        if self._dir is None:
            return
        try:
            self._sample_pod()
            path = self._file(os.getpid())
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot()))
            os.replace(tmp, path)
        except Exception as e:  # metrics must never hurt synthesis
            logger.debug(f"ElevenLabs live metrics publish failed: {e}")

    def _sample_pod(self) -> None:
        """Add the pod's CURRENT totals (this worker + the others' last
        snapshots) to the sampled pod peaks."""
        now = time.time()
        totals = dict(self._total)
        accounts = {f"{g}@{a}": v for (g, a), v in self._by_account.items()}
        others = self._others()
        for snap in others:
            for g, v in snap.get("totals", {}).items():
                totals[g] = totals.get(g, 0) + v
            for k, v in snap.get("by_account", {}).items():
                accounts[k] = accounts.get(k, 0) + v
        for k, v in {**totals, **accounts}.items():
            # The previous sample was held until now (see LiveStats.add).
            self._pod_peak(k).note(max(v, self._pod_last.get(k, 0)), now)
            self._pod_last[k] = v
        # Rates: the pod's count per second (recent seconds) and per minute.
        for r, rate in self._rates.items():
            per_sec = dict(rate.seconds(now))
            per_min = {_minute(now): rate.minutes.get(_minute(now), 0)}
            for snap in others:
                theirs = (snap.get("rates") or {}).get(r) or {}
                for s, v in (theirs.get("seconds") or {}).items():
                    per_sec[int(s)] = per_sec.get(int(s), 0) + v
                m = str(_minute(now))
                per_min[_minute(now)] += (theirs.get("minutes") or {}).get(m, 0)
            for s, v in per_sec.items():
                self._pod_peak(f"{r}/s").note(v, s)
            for m, v in per_min.items():
                if v:
                    self._pod_peak(f"{r}/min").note(v, m * 60)

    def _pod_peak(self, key: str) -> _Peak:
        peak = self._pod_peaks.get(key)
        if peak is None:
            peak = self._pod_peaks[key] = _Peak()
        return peak

    def _others(self) -> list[dict]:
        """The other live workers' last snapshots (dead workers' files are
        removed: uvicorn restarts a crashed worker under a new pid)."""
        if self._dir is None:
            return []
        out = []
        me = os.getpid()
        for path in self._dir.glob("worker-*.json"):
            try:
                pid = int(path.stem.split("-", 1)[1])
            except ValueError:
                continue
            if pid == me:
                continue
            if not _alive(pid):
                path.unlink(missing_ok=True)
                continue
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue  # mid-replace or gone: skip this read
        return out

    # -- snapshot ------------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.time()
        sockets: dict[str, int] = {}
        slots: dict[str, int] = {}
        for pool in list(self._pools()):
            counts = pool.live_counts()
            for k, v in counts["sockets"].items():
                sockets[k] = sockets.get(k, 0) + v
            for k, v in counts["slots"].items():
                slots[k] = slots.get(k, 0) + v
        budget = None
        if self._budget is not None:
            budget = {
                a.name: {
                    "open": self._budget.open.get(a.name, 0),
                    "share": self._budget.share.get(a.name, 0),
                    "max_websockets": a.max_websockets,
                    "traffic_percent": a.traffic_percent,
                }
                for a in self._budget.accounts
            }
        return {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "updated_at": now,
            "totals": dict(self._total),
            "by_account": {f"{g}@{a}": v for (g, a), v in self._by_account.items()},
            "by_model": {f"{g}|{m}|{a}": v for (g, m, a), v in self._cur.items()},
            "sockets": sockets,
            "slots": slots,
            "budget": budget,
            "counters": dict(self.counters),
            "rates": {r: rate.dump(now) for r, rate in self._rates.items()},
            "peaks": {k: p.dump(now) for k, p in self._peaks.items()},
            "pod_peaks": {k: p.dump(now) for k, p in self._pod_peaks.items()},
        }

    def pod(self) -> dict:
        """Everything the endpoint returns: this worker's fresh state plus the
        others' last snapshots, summed."""
        if self._dir is not None:
            self._sample_pod()
        return aggregate([self.snapshot(), *self._others()], time.time())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def default_dir() -> Path:
    """One directory per server: uvicorn workers share their master's pid as
    parent, so a restart (new master) never mixes with old files."""
    return Path(tempfile.gettempdir()) / f"dragontts-live-{os.getppid()}"


def _sum(snaps: list[dict], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in snaps:
        for k, v in (s.get(field) or {}).items():
            out[k] = out.get(k, 0) + v
    return out


def _merge_peaks(
    snaps: list[dict], field: str
) -> dict[str, tuple[dict[int, int], int]]:
    out: dict[str, tuple[dict[int, int], int]] = {}
    for s in snaps:
        for k, p in (s.get(field) or {}).items():
            buckets, top = out.get(k, ({}, 0))
            for m, v in p["buckets"].items():
                _record(buckets, int(m), v)
            out[k] = (buckets, max(top, p["all"]))
    return out


def aggregate(snaps: list[dict], now: float) -> dict:
    """Pod view from per-worker snapshots (pure; unit-tested)."""
    totals = _sum(snaps, "totals")
    by_account_cur = _sum(snaps, "by_account")
    sockets = _sum(snaps, "sockets")
    slots = _sum(snaps, "slots")
    pod_peaks = _merge_peaks(snaps, "pod_peaks")

    by_model: dict[str, dict[str, int]] = {}
    for k, v in _sum(snaps, "by_model").items():
        gauge, model, _account = k.split("|", 2)
        row = by_model.setdefault(model, dict.fromkeys(GAUGES, 0))
        row[gauge] += v

    accounts: dict[str, dict] = {}
    for k, v in by_account_cur.items():
        gauge, account = k.split("@", 1)
        accounts.setdefault(account, dict.fromkeys(GAUGES, 0))[gauge] = v
    budget = next((s["budget"] for s in snaps if s.get("budget")), None)
    for name, b in (budget or {}).items():
        row = accounts.setdefault(name, dict.fromkeys(GAUGES, 0))
        row["max_websockets"] = b["max_websockets"]
        row["traffic_percent"] = b["traffic_percent"]
        row["share_per_worker"] = b["share"]
        row["slots_held"] = sum(
            (s.get("budget") or {}).get(name, {}).get("open", 0) for s in snaps
        )

    def peak(key: str, current: int) -> dict[str, int]:
        buckets, top = pod_peaks.get(key, ({}, 0))
        return windows(buckets, current, top, now)

    for name, row in accounts.items():
        row["peak_sockets_open"] = peak(
            f"{SOCKETS_OPEN}@{name}", by_account_cur.get(f"{SOCKETS_OPEN}@{name}", 0)
        )

    rates = _rates(snaps, pod_peaks, now)
    used, total = slots.get("used", 0), slots.get("total", 0)
    return {
        "workers_reporting": len(snaps),
        "sockets": {"open": totals.get(SOCKETS_OPEN, 0), **sockets},
        "in_flight": {
            "requests": totals.get(REQUESTS_IN_FLIGHT, 0),
            "elevenlabs": totals.get(ELEVENLABS_IN_FLIGHT, 0),
            "websocket": totals.get(WS_IN_FLIGHT, 0),
            "http": totals.get(HTTP_IN_FLIGHT, 0),
        },
        "rates": rates,
        "cache": _hit_rate(rates),
        "slots": {
            "total": total,
            "used": used,
            "free": max(0, total - used),
            "utilization_pct": round(100 * used / total, 1) if total else 0.0,
        },
        "peaks": {g: peak(g, totals.get(g, 0)) for g in ALL_GAUGES},
        "by_model": by_model,
        "by_account": accounts,
        "counters": _sum(snaps, "counters"),
        "workers": [
            {
                "pid": s["pid"],
                "updated_ago_s": round(now - s["updated_at"], 1),
                "uptime_s": round(now - s["started_at"]),
                **{g: (s.get("totals") or {}).get(g, 0) for g in ALL_GAUGES},
                "peaks": {
                    g: windows(
                        {int(m): v for m, v in p["buckets"].items()},
                        (s.get("totals") or {}).get(g, 0),
                        p["all"],
                        now,
                    )
                    for g, p in (s.get("peaks") or {}).items()
                    if g in ALL_GAUGES
                },
            }
            for s in sorted(snaps, key=lambda s: s["pid"])
        ],
    }


def _rates(snaps: list[dict], pod_peaks: dict, now: float) -> dict[str, dict]:
    """Per rate: totals, this / last minute, and peaks per minute (exact over
    the last hour: every worker's per-minute counts summed) and per second
    (sampled pod-wide like the gauge peaks, and never below any single
    worker's own exact per-second peak)."""
    minute, second = _minute(now), int(now)
    out: dict[str, dict] = {}
    for r in RATES:
        rows = [(s.get("rates") or {}).get(r) or {} for s in snaps]
        per_min: dict[int, int] = {}
        this_sec = 0
        for row in rows:
            for m, v in (row.get("minutes") or {}).items():
                if int(m) > minute - _KEEP_MINUTES:
                    per_min[int(m)] = per_min.get(int(m), 0) + v
            this_sec += (row.get("seconds") or {}).get(str(second), 0)
        this_min = per_min.get(minute, 0)
        hour = max(per_min.values(), default=0)
        _, m_all = pod_peaks.get(f"{r}/min", ({}, 0))  # since start: sampled
        sampled, s_all = pod_peaks.get(f"{r}/s", ({}, 0))
        s_buckets = dict(sampled)
        for row in rows:
            own = row.get("second_peak") or {}
            for m, v in (own.get("buckets") or {}).items():
                _record(s_buckets, int(m), v)
            s_all = max(s_all, own.get("all", 0))
        out[r] = {
            "total": sum(row.get("total", 0) for row in rows),
            "this_minute": this_min,
            "last_minute": per_min.get(minute - 1, 0),
            "per_minute": {
                f"{w}m": sum(v for m, v in per_min.items() if m > minute - w)
                for w in WINDOWS_MIN
            },
            "peak_per_minute": windows(per_min, this_min, max(m_all, hour), now),
            "peak_per_second": windows(s_buckets, this_sec, s_all, now),
        }
    return out


def _hit_rate(rates: dict[str, dict]) -> dict:
    """Share of sentences served from the cache, per window and since start."""
    hits, misses = rates[CACHE_HITS], rates[CACHE_MISSES]

    def pct(h: int, m: int) -> float | None:
        return round(100 * h / (h + m), 1) if h + m else None

    return {
        "hit_rate_pct": {
            **{
                w: pct(hits["per_minute"][w], misses["per_minute"][w])
                for w in hits["per_minute"]
            },
            "since_start": pct(hits["total"], misses["total"]),
        }
    }


LIVE = LiveStats()
