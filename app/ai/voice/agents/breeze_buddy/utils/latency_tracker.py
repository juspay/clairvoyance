"""
Comprehensive Latency Tracking System

Tracks latency at each stage of the voice pipeline:
- STT (Speech-to-Text)
- LLM (Language Model)
- TTS (Text-to-Speech)
- Total end-to-end

Provides percentile analysis (P50, P95, P99) and exports metrics.

Based on Bolna's granular latency tracking approach.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
from loguru import logger


@dataclass
class ComponentLatency:
    """Latency data for a single component in a turn."""
    component: str
    turn_id: str
    sequence_id: Optional[int]
    first_byte_latency_ms: Optional[float] = None  # Time to first byte/token
    total_duration_ms: Optional[float] = None  # Total processing time
    metadata: Dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class TurnLatency:
    """Complete latency data for a conversation turn."""
    turn_id: str
    session_id: str
    stt_latency: Optional[ComponentLatency] = None
    llm_latency: Optional[ComponentLatency] = None
    tts_latency: Optional[ComponentLatency] = None
    total_latency_ms: Optional[float] = None
    turn_start_time: float = field(default_factory=time.time)
    turn_end_time: Optional[float] = None


class LatencyTracker:
    """
    Track and analyze latency across voice pipeline components.

    Provides detailed per-turn tracking and session-level statistics.
    """

    def __init__(self, session_id: str):
        """
        Initialize latency tracker for a session.

        Args:
            session_id: Unique session identifier
        """
        self.session_id = session_id
        self.session_start_time = time.time()

        # Turn-level tracking
        self.turns: Dict[str, TurnLatency] = {}
        self.current_turn_id: Optional[str] = None

        # Component-level aggregation
        self.component_latencies: Dict[str, List[ComponentLatency]] = defaultdict(list)

        # Connection latencies (one-time per session)
        self.connection_latencies = {
            "stt_connection_ms": None,
            "llm_connection_ms": None,
            "tts_connection_ms": None
        }

        logger.info(f"[Latency] Tracker initialized for session {session_id}")

    def start_turn(self, turn_id: str) -> None:
        """
        Start tracking a new conversation turn.

        Args:
            turn_id: Unique turn identifier
        """
        self.current_turn_id = turn_id
        self.turns[turn_id] = TurnLatency(
            turn_id=turn_id,
            session_id=self.session_id,
            turn_start_time=time.time()
        )
        logger.debug(f"[Latency] Turn started: {turn_id}")

    def end_turn(self, turn_id: Optional[str] = None) -> Optional[TurnLatency]:
        """
        End tracking for a turn and calculate total latency.

        Args:
            turn_id: Turn identifier (uses current if None)

        Returns:
            TurnLatency object with complete data
        """
        turn_id = turn_id or self.current_turn_id
        if not turn_id or turn_id not in self.turns:
            logger.warning(f"[Latency] Cannot end turn {turn_id}: not found")
            return None

        turn = self.turns[turn_id]
        turn.turn_end_time = time.time()

        # Calculate total turn latency
        turn.total_latency_ms = (turn.turn_end_time - turn.turn_start_time) * 1000

        logger.info(
            f"[Latency] Turn completed: {turn_id}, "
            f"total={turn.total_latency_ms:.0f}ms, "
            f"stt={turn.stt_latency.total_duration_ms if turn.stt_latency else 'N/A'}ms, "
            f"llm={turn.llm_latency.total_duration_ms if turn.llm_latency else 'N/A'}ms, "
            f"tts={turn.tts_latency.total_duration_ms if turn.tts_latency else 'N/A'}ms"
        )

        if turn_id == self.current_turn_id:
            self.current_turn_id = None

        return turn

    def track_component(
        self,
        component: str,
        first_byte_latency_ms: Optional[float] = None,
        total_duration_ms: Optional[float] = None,
        turn_id: Optional[str] = None,
        sequence_id: Optional[int] = None,
        metadata: Optional[Dict] = None
    ) -> ComponentLatency:
        """
        Track latency for a specific component.

        Args:
            component: Component name ("stt", "llm", "tts")
            first_byte_latency_ms: Time to first byte/token
            total_duration_ms: Total processing duration
            turn_id: Turn identifier (uses current if None)
            sequence_id: Sequence ID for this component execution
            metadata: Additional metadata (provider, model, etc.)

        Returns:
            ComponentLatency object
        """
        turn_id = turn_id or self.current_turn_id
        if not turn_id:
            logger.warning(f"[Latency] Cannot track {component}: no active turn")
            return None

        latency = ComponentLatency(
            component=component,
            turn_id=turn_id,
            sequence_id=sequence_id,
            first_byte_latency_ms=first_byte_latency_ms,
            total_duration_ms=total_duration_ms,
            metadata=metadata or {}
        )

        # Store in turn
        if turn_id in self.turns:
            turn = self.turns[turn_id]
            if component == "stt":
                turn.stt_latency = latency
            elif component == "llm":
                turn.llm_latency = latency
            elif component == "tts":
                turn.tts_latency = latency

        # Store in component aggregation
        self.component_latencies[component].append(latency)

        logger.debug(
            f"[Latency] {component.upper()} tracked for turn {turn_id}: "
            f"TTFB={first_byte_latency_ms:.0f}ms, total={total_duration_ms:.0f}ms"
        )

        return latency

    def track_connection(self, component: str, latency_ms: float) -> None:
        """
        Track initial connection latency for a component.

        Args:
            component: Component name ("stt", "llm", "tts")
            latency_ms: Connection establishment time in milliseconds
        """
        key = f"{component}_connection_ms"
        if key in self.connection_latencies:
            self.connection_latencies[key] = latency_ms
            logger.info(f"[Latency] {component.upper()} connection: {latency_ms:.0f}ms")

    def get_component_stats(self, component: str) -> Dict:
        """
        Get statistical analysis for a component.

        Args:
            component: Component name ("stt", "llm", "tts")

        Returns:
            Dictionary with statistics (p50, p95, p99, mean, etc.)
        """
        latencies = self.component_latencies.get(component, [])
        if not latencies:
            return {
                "component": component,
                "count": 0,
                "p50_ms": None,
                "p95_ms": None,
                "p99_ms": None,
                "mean_ms": None,
                "min_ms": None,
                "max_ms": None
            }

        # Extract TTFB and total duration
        ttfb_values = [l.first_byte_latency_ms for l in latencies if l.first_byte_latency_ms is not None]
        total_values = [l.total_duration_ms for l in latencies if l.total_duration_ms is not None]

        def calc_percentiles(values):
            if not values:
                return {}
            arr = np.array(values)
            return {
                "p50": float(np.percentile(arr, 50)),
                "p95": float(np.percentile(arr, 95)),
                "p99": float(np.percentile(arr, 99)),
                "mean": float(np.mean(arr)),
                "min": float(np.min(arr)),
                "max": float(np.max(arr))
            }

        return {
            "component": component,
            "count": len(latencies),
            "ttfb": calc_percentiles(ttfb_values),
            "total": calc_percentiles(total_values),
            "connection_ms": self.connection_latencies.get(f"{component}_connection_ms")
        }

    def get_session_summary(self) -> Dict:
        """
        Get comprehensive session latency summary.

        Returns:
            Dictionary with session-level statistics
        """
        total_latencies = [
            turn.total_latency_ms
            for turn in self.turns.values()
            if turn.total_latency_ms is not None
        ]

        summary = {
            "session_id": self.session_id,
            "session_duration_sec": time.time() - self.session_start_time,
            "total_turns": len(self.turns),
            "components": {
                "stt": self.get_component_stats("stt"),
                "llm": self.get_component_stats("llm"),
                "tts": self.get_component_stats("tts")
            },
            "end_to_end": {},
            "connection_latencies": self.connection_latencies
        }

        # Calculate end-to-end stats
        if total_latencies:
            arr = np.array(total_latencies)
            summary["end_to_end"] = {
                "count": len(total_latencies),
                "p50_ms": float(np.percentile(arr, 50)),
                "p95_ms": float(np.percentile(arr, 95)),
                "p99_ms": float(np.percentile(arr, 99)),
                "mean_ms": float(np.mean(arr)),
                "min_ms": float(np.min(arr)),
                "max_ms": float(np.max(arr))
            }

        return summary

    def export_to_langfuse(self, langfuse_client) -> None:
        """
        Export latency data to Langfuse for analysis.

        Args:
            langfuse_client: Langfuse client instance
        """
        try:
            summary = self.get_session_summary()

            # Log session-level metrics
            langfuse_client.score(
                name="latency_p95_ms",
                value=summary["end_to_end"].get("p95_ms", 0),
                trace_id=self.session_id
            )

            langfuse_client.score(
                name="latency_p50_ms",
                value=summary["end_to_end"].get("p50_ms", 0),
                trace_id=self.session_id
            )

            # Log component-level metrics
            for component, stats in summary["components"].items():
                if stats["count"] > 0:
                    langfuse_client.score(
                        name=f"{component}_latency_p95_ms",
                        value=stats["total"].get("p95", 0),
                        trace_id=self.session_id
                    )

            logger.info(f"[Latency] Exported to Langfuse for session {self.session_id}")

        except Exception as e:
            logger.error(f"[Latency] Failed to export to Langfuse: {e}")

    def log_summary(self) -> None:
        """Log session latency summary."""
        summary = self.get_session_summary()

        logger.info("=" * 80)
        logger.info(f"LATENCY SUMMARY - Session {self.session_id}")
        logger.info("=" * 80)
        logger.info(f"Total turns: {summary['total_turns']}")
        logger.info(f"Session duration: {summary['session_duration_sec']:.1f}s")
        logger.info("")

        # End-to-end
        if summary["end_to_end"]:
            e2e = summary["end_to_end"]
            logger.info("End-to-End Latency:")
            logger.info(f"  P50: {e2e['p50_ms']:.0f}ms")
            logger.info(f"  P95: {e2e['p95_ms']:.0f}ms")
            logger.info(f"  P99: {e2e['p99_ms']:.0f}ms")
            logger.info(f"  Mean: {e2e['mean_ms']:.0f}ms")
            logger.info(f"  Range: {e2e['min_ms']:.0f}ms - {e2e['max_ms']:.0f}ms")
            logger.info("")

        # Components
        for component, stats in summary["components"].items():
            if stats["count"] > 0:
                logger.info(f"{component.upper()} Latency:")
                if stats["ttfb"]:
                    logger.info(f"  TTFB P50: {stats['ttfb']['p50']:.0f}ms")
                if stats["total"]:
                    logger.info(f"  Total P50: {stats['total']['p50']:.0f}ms")
                    logger.info(f"  Total P95: {stats['total']['p95']:.0f}ms")
                if stats["connection_ms"]:
                    logger.info(f"  Connection: {stats['connection_ms']:.0f}ms")
                logger.info("")

        logger.info("=" * 80)
