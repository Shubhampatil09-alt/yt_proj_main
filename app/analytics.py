from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from .config import (
    EXECUTION_RETENTION_DAYS,
    RESOURCE_BATCH_SIZE,
    RESOURCE_BUFFER_SIZE,
    RESOURCE_RETENTION_DAYS,
)
from .database import JobHistory


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AnalyticsService:
    """Buffers resource samples and exposes stored analytics operations."""

    def __init__(self, store: JobHistory, session_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self._buffer: deque[dict[str, Any]] = deque(maxlen=RESOURCE_BUFFER_SIZE)
        self._lock = Lock()

    def record_resource_sample(
        self,
        *,
        cpu_percent: float,
        ram_percent: float,
        available_ram_gb: float,
        worker_state: dict[str, Any],
        performance: dict[str, float] | None = None,
    ) -> None:
        worker_memory = {
            str(bot["bot_id"]): float(bot["browser_memory_mb"])
            for bot in worker_state["bots"]
            if float(bot["browser_memory_mb"]) > 0
        }
        sample = {
            "session_id": self.session_id,
            "timestamp": utc_now(),
            "cpu_percent": float(cpu_percent),
            "ram_percent": float(ram_percent),
            "available_ram_gb": float(available_ram_gb),
            "worker_ram_mb": round(sum(worker_memory.values()) + float((performance or {}).get("youtube_memory_mb", 0)), 2),
            "active_worker_count": int(worker_state.get("total_active_browsers", worker_state["count"])),
            "worker_memory": worker_memory,
            "backend_rss_mb": float((performance or {}).get("backend_rss_mb", 0)),
            "chrome_memory_mb": float((performance or {}).get("chrome_memory_mb", 0)),
            "chromedriver_memory_mb": float((performance or {}).get("chromedriver_memory_mb", 0)),
            "youtube_memory_mb": float((performance or {}).get("youtube_memory_mb", 0)),
        }
        batch = None
        with self._lock:
            self._buffer.append(sample)
            if len(self._buffer) >= RESOURCE_BATCH_SIZE:
                batch = list(self._buffer)
                self._buffer.clear()
        if batch:
            try:
                self.store.insert_resource_samples(batch)
            except Exception:
                # A transient database lock must never stop the safety monitor.
                with self._lock:
                    for buffered in batch:
                        self._buffer.append(buffered)

    def flush(self) -> int:
        with self._lock:
            batch = list(self._buffer)
            self._buffer.clear()
        try:
            return self.store.insert_resource_samples(batch)
        except Exception:
            with self._lock:
                for sample in batch:
                    self._buffer.append(sample)
            raise

    def cleanup_retention(self) -> dict[str, int]:
        self.flush()
        return self.store.cleanup_retention(
            self.session_id,
            resource_days=RESOURCE_RETENTION_DAYS,
            run_days=EXECUTION_RETENTION_DAYS,
        )

    @property
    def buffered_count(self) -> int:
        with self._lock:
            return len(self._buffer)
