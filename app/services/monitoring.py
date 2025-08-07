import asyncio
import time
from typing import Dict, Any, List
from dataclasses import dataclass, field
from collections import defaultdict, deque

from app.core.logger import logger
from app.core.redis_manager import redis_manager


@dataclass
class MetricPoint:
    timestamp: float
    value: float
    labels: Dict[str, str] = field(default_factory=dict)


class MetricsCollector:
    """Collects and aggregates performance metrics."""
    
    def __init__(self, max_points: int = 1000):
        self.max_points = max_points
        self.metrics: Dict[str, deque] = defaultdict(lambda: deque(maxlen=max_points))
        self._start_time = time.time()
    
    def record(self, metric_name: str, value: float, labels: Dict[str, str] = None):
        """Record a metric point."""
        point = MetricPoint(
            timestamp=time.time(),
            value=value,
            labels=labels or {}
        )
        self.metrics[metric_name].append(point)
    
    def get_metric(self, metric_name: str, since: float = None) -> List[MetricPoint]:
        """Get metric points since a timestamp."""
        points = list(self.metrics[metric_name])
        if since:
            points = [p for p in points if p.timestamp >= since]
        return points
    
    def get_latest(self, metric_name: str) -> MetricPoint:
        """Get the latest metric point."""
        points = self.metrics[metric_name]
        return points[-1] if points else None
    
    def get_average(self, metric_name: str, since: float = None) -> float:
        """Get average value since timestamp."""
        points = self.get_metric(metric_name, since)
        if not points:
            return 0.0
        return sum(p.value for p in points) / len(points)
    
    def get_summary(self) -> Dict[str, Any]:
        """Get summary of all metrics."""
        now = time.time()
        last_minute = now - 60
        last_hour = now - 3600
        
        summary = {
            "uptime": now - self._start_time,
            "total_metrics": len(self.metrics),
            "metrics": {}
        }
        
        for metric_name, points in self.metrics.items():
            if not points:
                continue
                
            latest = points[-1]
            minute_points = [p for p in points if p.timestamp >= last_minute]
            hour_points = [p for p in points if p.timestamp >= last_hour]
            
            summary["metrics"][metric_name] = {
                "latest": latest.value,
                "latest_time": latest.timestamp,
                "last_minute_count": len(minute_points),
                "last_minute_avg": sum(p.value for p in minute_points) / len(minute_points) if minute_points else 0,
                "last_hour_count": len(hour_points),
                "last_hour_avg": sum(p.value for p in hour_points) / len(hour_points) if hour_points else 0
            }
        
        return summary


class PerformanceMonitor:
    """Monitors system performance and health."""
    
    def __init__(self):
        self.metrics = MetricsCollector()
        self._monitoring = False
        self._monitor_task = None
    
    async def start(self):
        """Start performance monitoring."""
        if self._monitoring:
            return
            
        logger.info("Starting performance monitoring...")
        self._monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
    
    async def stop(self):
        """Stop performance monitoring."""
        if not self._monitoring:
            return
            
        logger.info("Stopping performance monitoring...")
        self._monitoring = False
        
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
    
    async def _monitor_loop(self):
        """Main monitoring loop."""
        while self._monitoring:
            try:
                await self._collect_system_metrics()
                await self._collect_redis_metrics()
                await self._collect_worker_metrics()
                await asyncio.sleep(10)  # Collect every 10 seconds
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(5)
    
    async def _collect_system_metrics(self):
        """Collect system-level metrics."""
        import psutil
        
        # CPU and Memory
        cpu_percent = psutil.cpu_percent()
        memory = psutil.virtual_memory()
        
        self.metrics.record("cpu_percent", cpu_percent)
        self.metrics.record("memory_percent", memory.percent)
        self.metrics.record("memory_available_mb", memory.available / 1024 / 1024)
        
        # Process info
        process = psutil.Process()
        self.metrics.record("process_memory_mb", process.memory_info().rss / 1024 / 1024)
        self.metrics.record("process_cpu_percent", process.cpu_percent())
        self.metrics.record("process_threads", process.num_threads())
    
    async def _collect_redis_metrics(self):
        """Collect Redis metrics."""
        try:
            # Redis info
            info = await redis_manager.redis.info()
            
            self.metrics.record("redis_connected_clients", info.get("connected_clients", 0))
            self.metrics.record("redis_used_memory_mb", info.get("used_memory", 0) / 1024 / 1024)
            self.metrics.record("redis_ops_per_sec", info.get("instantaneous_ops_per_sec", 0))
            
            # Queue lengths
            session_queue_len = await redis_manager.redis.llen("voice_sessions")
            response_queue_len = await redis_manager.redis.llen("worker_responses")
            
            self.metrics.record("session_queue_length", session_queue_len)
            self.metrics.record("response_queue_length", response_queue_len)
            
        except Exception as e:
            logger.error(f"Failed to collect Redis metrics: {e}")
    
    async def _collect_worker_metrics(self):
        """Collect worker pool metrics."""
        try:
            from app.services.worker_pool import worker_pool_manager
            from app.services.session_manager import session_manager
            
            # Worker stats
            worker_stats = await worker_pool_manager.get_worker_stats()
            session_stats = await session_manager.get_session_stats()
            
            self.metrics.record("total_workers", worker_stats["total_workers"])
            self.metrics.record("ready_workers", worker_stats["ready_workers"])
            self.metrics.record("total_sessions", worker_stats["total_sessions"])
            
            # Session stats by type
            for session_type, count in session_stats.get("by_type", {}).items():
                self.metrics.record("sessions_by_type", count, {"type": session_type})
            
            # Session stats by status
            for status, count in session_stats.get("by_status", {}).items():
                self.metrics.record("sessions_by_status", count, {"status": status})
                
        except Exception as e:
            logger.error(f"Failed to collect worker metrics: {e}")
    
    def record_session_event(self, event_type: str, session_type: str = None, duration: float = None):
        """Record session-related events."""
        labels = {}
        if session_type:
            labels["session_type"] = session_type
            
        self.metrics.record(f"session_{event_type}", 1, labels)
        
        if duration is not None:
            self.metrics.record(f"session_{event_type}_duration", duration, labels)
    
    def record_worker_event(self, event_type: str, worker_id: str = None):
        """Record worker-related events."""
        labels = {}
        if worker_id:
            labels["worker_id"] = worker_id
            
        self.metrics.record(f"worker_{event_type}", 1, labels)
    
    async def get_dashboard_data(self) -> Dict[str, Any]:
        """Get data for monitoring dashboard."""
        try:
            from app.services.worker_pool import worker_pool_manager
            from app.services.session_manager import session_manager
            from app.services.model_manager import shared_model_manager
            
            # Current stats
            worker_stats = await worker_pool_manager.get_worker_stats()
            session_stats = await session_manager.get_session_stats()
            model_stats = await shared_model_manager.get_model_stats()
            
            # Performance metrics
            metrics_summary = self.metrics.get_summary()
            
            # Recent activity
            now = time.time()
            last_minute = now - 60
            
            recent_sessions = len(self.metrics.get_metric("session_created", last_minute))
            recent_completions = len(self.metrics.get_metric("session_completed", last_minute))
            
            return {
                "timestamp": now,
                "system": {
                    "uptime": metrics_summary["uptime"],
                    "cpu_percent": self.metrics.get_latest("cpu_percent").value if self.metrics.get_latest("cpu_percent") else 0,
                    "memory_percent": self.metrics.get_latest("memory_percent").value if self.metrics.get_latest("memory_percent") else 0,
                    "process_memory_mb": self.metrics.get_latest("process_memory_mb").value if self.metrics.get_latest("process_memory_mb") else 0
                },
                "workers": worker_stats,
                "sessions": session_stats,
                "models": model_stats,
                "redis": {
                    "connected": True,
                    "session_queue_length": self.metrics.get_latest("session_queue_length").value if self.metrics.get_latest("session_queue_length") else 0,
                    "response_queue_length": self.metrics.get_latest("response_queue_length").value if self.metrics.get_latest("response_queue_length") else 0
                },
                "activity": {
                    "sessions_created_last_minute": recent_sessions,
                    "sessions_completed_last_minute": recent_completions,
                    "avg_session_duration": self.metrics.get_average("session_completed_duration", last_minute)
                },
                "metrics": metrics_summary
            }
            
        except Exception as e:
            logger.error(f"Failed to get dashboard data: {e}")
            return {"error": str(e)}


# Global performance monitor instance
performance_monitor = PerformanceMonitor()