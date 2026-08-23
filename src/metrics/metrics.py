"""Metrics tracker for observability (Grafana / Better Stack / Prometheus).

Tracks:
  - Scan duration (per exchange + total)
  - Errors per exchange (count, last error, timestamp)
  - Signals per hour (sliding window)
  - Uptime
  - WS connection status
  - API request counts

Exposes:
  - /api/metrics → Prometheus text format
  - /api/metrics/summary → Human-readable JSON
"""

import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExchangeMetrics:
    """Metrics for a single exchange."""
    name: str
    scan_count: int = 0
    scan_duration_total: float = 0.0    # seconds
    scan_duration_last: float = 0.0     # seconds
    scan_duration_avg: float = 0.0
    error_count: int = 0
    last_error: str = ""
    last_error_time: Optional[float] = None
    symbols_scanned_last: int = 0
    signals_found_last: int = 0
    success_rate: float = 100.0         # percentage

    def record_scan(self, duration: float, symbols: int, signals: int):
        self.scan_count += 1
        self.scan_duration_total += duration
        self.scan_duration_last = duration
        self.scan_duration_avg = self.scan_duration_total / self.scan_count
        self.symbols_scanned_last = symbols
        self.signals_found_last = signals

    def record_error(self, error: str):
        self.error_count += 1
        self.last_error = error[:200]
        self.last_error_time = time.time()
        if self.scan_count > 0:
            self.success_rate = ((self.scan_count - self.error_count) / self.scan_count) * 100

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "scan_count": self.scan_count,
            "scan_duration_last_s": round(self.scan_duration_last, 2),
            "scan_duration_avg_s": round(self.scan_duration_avg, 2),
            "scan_duration_total_s": round(self.scan_duration_total, 1),
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_error_time": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.last_error_time))
                if self.last_error_time else None
            ),
            "symbols_scanned_last": self.symbols_scanned_last,
            "signals_found_last": self.signals_found_last,
            "success_rate_pct": round(self.success_rate, 1),
        }


class MetricsTracker:
    """Central metrics tracker for the pump detector."""

    def __init__(self, exchanges: List[str]):
        self.start_time = time.time()
        self.exchanges: Dict[str, ExchangeMetrics] = {
            name: ExchangeMetrics(name=name) for name in exchanges
        }
        self.total_signals_sent: int = 0
        self.total_signals_detected: int = 0
        self._signal_timestamps: deque = deque(maxlen=10000)
        self.api_requests: Dict[str, int] = {}  # endpoint -> count

    def record_scan(
        self,
        exchange: str,
        duration: float,
        symbols: int,
        signals: int,
    ):
        """Record a completed exchange scan."""
        if exchange not in self.exchanges:
            self.exchanges[exchange] = ExchangeMetrics(name=exchange)
        self.exchanges[exchange].record_scan(duration, symbols, signals)
        self.total_signals_detected += signals

    def record_scan_error(self, exchange: str, error: str):
        """Record a scan error."""
        if exchange not in self.exchanges:
            self.exchanges[exchange] = ExchangeMetrics(name=exchange)
        self.exchanges[exchange].record_error(error)

    def record_signals_sent(self, count: int):
        """Record signals sent to Telegram."""
        self.total_signals_sent += count
        now = time.time()
        for _ in range(count):
            self._signal_timestamps.append(now)

    def record_api_request(self, endpoint: str):
        """Record an API request."""
        self.api_requests[endpoint] = self.api_requests.get(endpoint, 0) + 1

    def signals_per_hour(self) -> float:
        """Calculate signals per hour from sliding window."""
        now = time.time()
        cutoff = now - 3600  # last hour
        recent = sum(1 for ts in self._signal_timestamps if ts > cutoff)
        return round(recent, 1)

    def signals_per_day(self) -> float:
        """Estimate signals per day from last hour."""
        return round(self.signals_per_hour() * 24, 0)

    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    def uptime_human(self) -> str:
        secs = int(self.uptime_seconds())
        days, secs = divmod(secs, 86400)
        hours, secs = divmod(secs, 3600)
        minutes, _ = divmod(secs, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    def total_errors(self) -> int:
        return sum(ex.error_count for ex in self.exchanges.values())

    def avg_scan_duration(self) -> float:
        durations = [ex.scan_duration_last for ex in self.exchanges.values() if ex.scan_duration_last > 0]
        return round(sum(durations) / len(durations), 2) if durations else 0

    def worst_exchange(self) -> Optional[str]:
        """Exchange with most errors or worst success rate."""
        worst = None
        worst_score = -1
        for name, ex in self.exchanges.items():
            score = ex.error_count + (100 - ex.success_rate) / 10
            if score > worst_score:
                worst_score = score
                worst = name
        return worst

    # ── Prometheus format ───────────────────────────────────────────────

    def to_prometheus(self) -> str:
        """Export metrics in Prometheus text format."""
        lines = []
        ts = int(time.time() * 1000)

        # Uptime
        lines.append(f'pump_detector_uptime_seconds {self.uptime_seconds():.0f} {ts}')

        # Per-exchange metrics
        for name, ex in self.exchanges.items():
            prefix = f'pump_detector_exchange_{name}'
            lines.append(f'{prefix}_scan_count {ex.scan_count} {ts}')
            lines.append(f'{prefix}_scan_duration_last_seconds {ex.scan_duration_last:.3f} {ts}')
            lines.append(f'{prefix}_scan_duration_avg_seconds {ex.scan_duration_avg:.3f} {ts}')
            lines.append(f'{prefix}_error_count {ex.error_count} {ts}')
            lines.append(f'{prefix}_success_rate {ex.success_rate:.1f} {ts}')
            lines.append(f'{prefix}_symbols_scanned {ex.symbols_scanned_last} {ts}')
            lines.append(f'{prefix}_signals_found {ex.signals_found_last} {ts}')

        # Global metrics
        lines.append(f'pump_detector_signals_total {self.total_signals_detected} {ts}')
        lines.append(f'pump_detector_signals_sent_total {self.total_signals_sent} {ts}')
        lines.append(f'pump_detector_signals_per_hour {self.signals_per_hour()} {ts}')
        lines.append(f'pump_detector_errors_total {self.total_errors()} {ts}')
        lines.append(f'pump_detector_avg_scan_duration_seconds {self.avg_scan_duration()} {ts}')

        # API requests
        for endpoint, count in self.api_requests.items():
            safe = endpoint.replace("/", "_").replace("?", "_").strip("_")
            lines.append(f'pump_detector_api_requests{{endpoint="{endpoint}"}} {count} {ts}')

        return "\n".join(lines) + "\n"

    # ── JSON summary ───────────────────────────────────────────────────

    def to_summary(self) -> dict:
        """Human-readable JSON summary for dashboard."""
        return {
            "uptime": self.uptime_human(),
            "uptime_seconds": round(self.uptime_seconds()),
            "started_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.start_time)
            ),
            "exchanges": {
                name: ex.to_dict() for name, ex in self.exchanges.items()
            },
            "totals": {
                "signals_detected": self.total_signals_detected,
                "signals_sent": self.total_signals_sent,
                "signals_per_hour": self.signals_per_hour(),
                "signals_per_day_est": self.signals_per_day(),
                "errors": self.total_errors(),
                "avg_scan_duration_s": self.avg_scan_duration(),
            },
            "api_requests": dict(self.api_requests),
            "worst_exchange": self.worst_exchange(),
        }

    # ── Health status ──────────────────────────────────────────────────

    def health_status(self) -> str:
        """Overall health: healthy / degraded / critical."""
        if self.total_errors() == 0:
            return "healthy"
        worst = self.worst_exchange()
        if worst and self.exchanges[worst].success_rate < 50:
            return "critical"
        if self.total_errors() > 10:
            return "degraded"
        return "healthy"
