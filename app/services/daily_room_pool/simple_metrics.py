"""
Simplified metrics for room pool - no complex time-series tracking.
"""

import time
from collections import defaultdict


class SimplePoolMetrics:
    """Basic metrics without complex time-series tracking"""

    def __init__(self):
        self.counters = defaultdict(int)
        self.start_time = time.time()

    def record_pool_hit(self):
        """Record successful room served from pool"""
        self.counters['rooms_served'] += 1

    def record_fallback_used(self):
        """Record fallback creation used"""
        self.counters['fallback_used'] += 1

    def record_room_created(self):
        """Record room creation (pool or fallback)"""
        self.counters['rooms_created'] += 1

    def record_creation_error(self):
        """Record room creation error"""
        self.counters['creation_errors'] += 1

    def record_expired_cleaned(self, count: int):
        """Record expired rooms cleaned"""
        self.counters['expired_cleaned'] += count

    def get_pool_hit_rate(self) -> float:
        """Calculate pool hit rate percentage"""
        served = self.counters['rooms_served']
        fallback = self.counters['fallback_used']
        total = served + fallback

        if total == 0:
            return 100.0
        return (served / total) * 100

    def get_uptime_hours(self) -> float:
        """Get service uptime in hours"""
        return (time.time() - self.start_time) / 3600

    def get_stats_dict(self) -> dict:
        """Get all metrics as dictionary"""
        return {
            'uptime_hours': round(self.get_uptime_hours(), 2),
            'pool_hit_rate_pct': round(self.get_pool_hit_rate(), 2),
            **dict(self.counters)
        }

    def reset_counters(self):
        """Reset all counters (for testing)"""
        self.counters.clear()
        self.start_time = time.time()