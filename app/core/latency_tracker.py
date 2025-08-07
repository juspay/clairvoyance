import time
import asyncio
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from collections import defaultdict

from app.core.logger import logger


@dataclass
class LatencyEvent:
    event_name: str
    start_time: float
    end_time: Optional[float] = None
    duration_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def finish(self, metadata: Dict[str, Any] = None):
        """Mark the event as finished and calculate duration."""
        self.end_time = time.time()
        self.duration_ms = (self.end_time - self.start_time) * 1000
        if metadata:
            self.metadata.update(metadata)


class LatencyTracker:
    """Tracks latency metrics for user sessions."""
    
    def __init__(self):
        self.active_events: Dict[str, Dict[str, LatencyEvent]] = defaultdict(dict)
        self.completed_events: List[LatencyEvent] = []
        self.session_timelines: Dict[str, List[LatencyEvent]] = defaultdict(list)
    
    def start_event(self, session_id: str, event_name: str, metadata: Dict[str, Any] = None) -> str:
        """Start tracking a latency event."""
        event_key = f"{event_name}_{int(time.time() * 1000000)}"  # Add microsecond precision
        
        event = LatencyEvent(
            event_name=event_name,
            start_time=time.time(),
            metadata=metadata or {}
        )
        
        self.active_events[session_id][event_key] = event
        
        logger.info(
            f"[LATENCY] Session {session_id} | Started: {event_name}",
            extra={
                "session_id": session_id,
                "event": event_name,
                "timestamp": event.start_time,
                "metadata": event.metadata
            }
        )
        
        return event_key
    
    def finish_event(self, session_id: str, event_key: str, metadata: Dict[str, Any] = None):
        """Finish tracking a latency event."""
        if session_id not in self.active_events:
            return
        
        if event_key not in self.active_events[session_id]:
            return
        
        event = self.active_events[session_id].pop(event_key)
        event.finish(metadata)
        
        # Store completed event
        self.completed_events.append(event)
        self.session_timelines[session_id].append(event)
        
        # Log with detailed timing
        logger.info(
            f"[LATENCY] Session {session_id} | Completed: {event.event_name} | Duration: {event.duration_ms:.2f}ms",
            extra={
                "session_id": session_id,
                "event": event.event_name,
                "duration_ms": event.duration_ms,
                "start_time": event.start_time,
                "end_time": event.end_time,
                "metadata": event.metadata
            }
        )
        
        # Log performance thresholds
        self._check_performance_thresholds(session_id, event)
    
    def _check_performance_thresholds(self, session_id: str, event: LatencyEvent):
        """Check if event duration exceeds performance thresholds."""
        thresholds = {
            "session_creation": 1000,  # 1 second
            "worker_allocation": 500,   # 500ms
            "model_loading": 2000,     # 2 seconds
            "bot_startup": 3000,       # 3 seconds
            "websocket_handshake": 200, # 200ms
            "api_call": 1000,          # 1 second
            "room_creation": 2000,     # 2 seconds
            "token_generation": 1000,  # 1 second
        }
        
        threshold = thresholds.get(event.event_name, 5000)  # Default 5 seconds
        
        if event.duration_ms > threshold:
            logger.warning(
                f"[LATENCY WARNING] Session {session_id} | {event.event_name} exceeded threshold | "
                f"Duration: {event.duration_ms:.2f}ms | Threshold: {threshold}ms",
                extra={
                    "session_id": session_id,
                    "event": event.event_name,
                    "duration_ms": event.duration_ms,
                    "threshold_ms": threshold,
                    "threshold_exceeded": True,
                    "metadata": event.metadata
                }
            )
    
    @asynccontextmanager
    async def track_async(self, session_id: str, event_name: str, metadata: Dict[str, Any] = None):
        """Context manager for tracking async operations."""
        event_key = self.start_event(session_id, event_name, metadata)
        try:
            yield
        except Exception as e:
            # Track errors in metadata
            error_metadata = {"error": str(e), "error_type": type(e).__name__}
            if metadata:
                error_metadata.update(metadata)
            self.finish_event(session_id, event_key, error_metadata)
            raise
        else:
            self.finish_event(session_id, event_key, metadata)
    
    def track_sync(self, session_id: str, event_name: str, metadata: Dict[str, Any] = None):
        """Decorator for tracking synchronous functions."""
        def decorator(func):
            def wrapper(*args, **kwargs):
                event_key = self.start_event(session_id, event_name, metadata)
                try:
                    result = func(*args, **kwargs)
                    self.finish_event(session_id, event_key, metadata)
                    return result
                except Exception as e:
                    error_metadata = {"error": str(e), "error_type": type(e).__name__}
                    if metadata:
                        error_metadata.update(metadata)
                    self.finish_event(session_id, event_key, error_metadata)
                    raise
            return wrapper
        return decorator
    
    def get_session_timeline(self, session_id: str) -> List[Dict[str, Any]]:
        """Get chronological timeline of events for a session."""
        events = self.session_timelines.get(session_id, [])
        return [
            {
                "event": event.event_name,
                "start_time": event.start_time,
                "end_time": event.end_time,
                "duration_ms": event.duration_ms,
                "metadata": event.metadata
            }
            for event in sorted(events, key=lambda e: e.start_time)
        ]
    
    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        """Get latency summary for a session."""
        events = self.session_timelines.get(session_id, [])
        if not events:
            return {"session_id": session_id, "total_events": 0}
        
        total_duration = 0
        event_durations = {}
        critical_path = []
        
        for event in events:
            if event.duration_ms:
                total_duration += event.duration_ms
                event_durations[event.event_name] = event.duration_ms
                
                # Track critical path events
                if event.event_name in ["session_creation", "worker_allocation", "bot_startup"]:
                    critical_path.append({
                        "event": event.event_name,
                        "duration_ms": event.duration_ms
                    })
        
        return {
            "session_id": session_id,
            "total_events": len(events),
            "total_duration_ms": total_duration,
            "critical_path_ms": sum(e["duration_ms"] for e in critical_path),
            "event_durations": event_durations,
            "critical_path": critical_path,
            "first_event": events[0].start_time if events else None,
            "last_event": events[-1].end_time if events else None
        }
    
    def get_performance_stats(self, time_window_minutes: int = 60) -> Dict[str, Any]:
        """Get performance statistics for recent events."""
        cutoff_time = time.time() - (time_window_minutes * 60)
        recent_events = [e for e in self.completed_events if e.start_time >= cutoff_time]
        
        if not recent_events:
            return {"time_window_minutes": time_window_minutes, "total_events": 0}
        
        # Group by event type
        by_event_type = defaultdict(list)
        for event in recent_events:
            if event.duration_ms:
                by_event_type[event.event_name].append(event.duration_ms)
        
        # Calculate statistics
        stats = {}
        for event_type, durations in by_event_type.items():
            stats[event_type] = {
                "count": len(durations),
                "avg_ms": sum(durations) / len(durations),
                "min_ms": min(durations),
                "max_ms": max(durations),
                "p95_ms": self._percentile(durations, 95),
                "p99_ms": self._percentile(durations, 99)
            }
        
        return {
            "time_window_minutes": time_window_minutes,
            "total_events": len(recent_events),
            "by_event_type": stats
        }
    
    def _percentile(self, data: List[float], percentile: int) -> float:
        """Calculate percentile of a list of numbers."""
        if not data:
            return 0.0
        sorted_data = sorted(data)
        index = int((percentile / 100) * len(sorted_data))
        if index >= len(sorted_data):
            index = len(sorted_data) - 1
        return sorted_data[index]
    
    def cleanup_session(self, session_id: str):
        """Clean up tracking data for a completed session."""
        # Remove any remaining active events
        self.active_events.pop(session_id, None)
        
        # Log final session summary
        summary = self.get_session_summary(session_id)
        logger.info(
            f"[LATENCY SUMMARY] Session {session_id} | Total Duration: {summary.get('total_duration_ms', 0):.2f}ms | "
            f"Critical Path: {summary.get('critical_path_ms', 0):.2f}ms | Events: {summary.get('total_events', 0)}",
            extra={
                "session_id": session_id,
                "latency_summary": summary
            }
        )


# Global latency tracker instance
latency_tracker = LatencyTracker()