import time
from collections import deque
from threading import Lock
from typing import Protocol

import psutil

from .config import (
    AVAILABLE_RAM_HYSTERESIS_GB,
    CPU_SUSTAINED_PERCENT,
    CPU_SUSTAINED_SAMPLES,
    MIN_AVAILABLE_RAM_GB,
    RAM_SHUTDOWN_PERCENT,
    RAM_START_BLOCK_PERCENT,
    SAFETY_HYSTERESIS_PERCENT,
)
from .database import JobHistory
from .analytics import AnalyticsService
from .workers import WorkerManager


class MetricsProvider(Protocol):
    def cpu_percent(self, interval: float | None = None) -> float: ...
    def virtual_memory(self): ...


class DashboardService:
    def __init__(
        self,
        workers: WorkerManager,
        history: JobHistory,
        metrics_provider: MetricsProvider = psutil,
        analytics: AnalyticsService | None = None,
    ) -> None:
        self.workers = workers
        self.history = history
        self.metrics_provider = metrics_provider
        self.analytics = analytics
        self.started_at = time.monotonic()
        self.cpu_samples: deque[float] = deque(maxlen=CPU_SUSTAINED_SAMPLES)
        self._metrics_lock = Lock()
        self.background_sampling = False
        self._latest_metrics: dict | None = None
        self.youtube_diagnostic = None
        self.metrics_provider.cpu_percent(interval=None)

    def metrics(self) -> dict:
        with self._metrics_lock:
            if self.background_sampling and self._latest_metrics is not None:
                return dict(self._latest_metrics)
        return self.sample_metrics()

    def sample_metrics(self) -> dict:
        with self._metrics_lock:
            self._latest_metrics = self._metrics_unlocked()
            return dict(self._latest_metrics)

    def _metrics_unlocked(self) -> dict:
        cpu = round(float(self.metrics_provider.cpu_percent(interval=None)), 1)
        memory = self.metrics_provider.virtual_memory()
        ram = round(float(memory.percent), 1)
        available_ram_gb = round(memory.available / 1024**3, 2)
        self.cpu_samples.append(cpu)
        sustained_cpu = (
            len(self.cpu_samples) == CPU_SUSTAINED_SAMPLES
            and all(value >= CPU_SUSTAINED_PERCENT for value in self.cpu_samples)
        )

        reason = None
        stop_workers = False
        if ram >= RAM_SHUTDOWN_PERCENT:
            reason = (
                f"Safety shutdown: RAM is {ram:.1f}% "
                f"(shutdown threshold {RAM_SHUTDOWN_PERCENT:.0f}%)."
            )
            stop_workers = True
        elif sustained_cpu:
            reason = (
                f"Safety shutdown: CPU remained above {CPU_SUSTAINED_PERCENT:.0f}% "
                f"for {CPU_SUSTAINED_SAMPLES} samples."
            )
            stop_workers = True
        elif self.workers.safety_latched:
            recovered = (
                ram < RAM_START_BLOCK_PERCENT - SAFETY_HYSTERESIS_PERCENT
                and available_ram_gb >= MIN_AVAILABLE_RAM_GB + AVAILABLE_RAM_HYSTERESIS_GB
                and cpu < CPU_SUSTAINED_PERCENT
            )
            if not recovered:
                reason = "Safety recovery lock: waiting for CPU and RAM hysteresis margins."
        elif ram >= RAM_START_BLOCK_PERCENT:
            reason = (
                f"Worker starts blocked: RAM is {ram:.1f}% "
                f"(start limit {RAM_START_BLOCK_PERCENT:.0f}%)."
            )
        elif available_ram_gb < MIN_AVAILABLE_RAM_GB:
            reason = (
                f"Worker starts blocked: only {available_ram_gb:.2f} GB RAM is available "
                f"(minimum {MIN_AVAILABLE_RAM_GB:.2f} GB)."
            )
        self.workers.update_safety(
            reason, stop_workers=stop_workers,
            latched=True if stop_workers else (False if reason is None else None),
        )

        counts = self.history.counts()
        worker_state = self.workers.state()
        chrome_memory = sum(
            item["process"].get("chrome_memory_mb", item["browser_memory_mb"])
            for item in worker_state["bots"]
        )
        driver_memory = sum(
            item["process"].get("chromedriver_memory_mb", 0)
            for item in worker_state["bots"]
        )
        youtube_state = self.youtube_diagnostic.snapshot() if self.youtube_diagnostic else None
        youtube_process = (youtube_state or {}).get("process", {})
        youtube_memory = float(youtube_process.get("memory_mb", 0))
        chrome_memory += float(youtube_process.get("chrome_memory_mb", 0))
        driver_memory += float(youtube_process.get("chromedriver_memory_mb", 0))
        worker_memory = chrome_memory + driver_memory
        try:
            backend_rss = psutil.Process().memory_info().rss / 1024**2
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            backend_rss = 0.0
        performance = {
            "backend_rss_mb": round(backend_rss, 1),
            "chrome_memory_mb": round(chrome_memory, 1),
            "chromedriver_memory_mb": round(driver_memory, 1),
            "youtube_memory_mb": round(youtube_memory, 1),
        }
        if self.analytics is not None:
            self.analytics.record_resource_sample(
                cpu_percent=cpu,
                ram_percent=ram,
                available_ram_gb=available_ram_gb,
                worker_state=worker_state,
                performance=performance,
            )

        return {
            "cpu_percent": cpu,
            "ram_percent": ram,
            "ram_used_gb": round((memory.total - memory.available) / 1024**3, 2),
            "ram_total_gb": round(memory.total / 1024**3, 2),
            "ram_available_gb": available_ram_gb,
            "worker_memory_mb": round(worker_memory, 1),
            **performance,
            "uptime_seconds": int(time.monotonic() - self.started_at),
            "completed_tests": counts["completed"],
            "failed_tests": counts["failed"],
            "active_workers": worker_state["count"],
            "ram_warning": ram >= RAM_START_BLOCK_PERCENT,
            "safety_shutdown": stop_workers,
            "start_blocked": reason is not None,
            "cpu_sustained": sustained_cpu,
            "thresholds": {
                "ram_start_block": RAM_START_BLOCK_PERCENT,
                "ram_shutdown": RAM_SHUTDOWN_PERCENT,
                "cpu_sustained": CPU_SUSTAINED_PERCENT,
                "cpu_samples": CPU_SUSTAINED_SAMPLES,
                "minimum_available_ram_gb": MIN_AVAILABLE_RAM_GB,
                "hysteresis_percent": SAFETY_HYSTERESIS_PERCENT,
            },
            "alerts": self._alerts(ram, available_ram_gb, sustained_cpu),
        }

    def performance(self) -> dict:
        metrics = self.metrics()
        state = self.workers.state()
        cleanup_failures = [
            {"worker_id": worker["id"], "orphaned_pids": worker["process"].get("orphaned_pids", [])}
            for worker in state["workers"]
            if worker["process"].get("cleanup_verified") is False
        ]
        if self.youtube_diagnostic:
            diagnostic = self.youtube_diagnostic.snapshot()
            if diagnostic.get("cleanup_verified") is False:
                cleanup_failures.append({
                    "diagnostic_id": diagnostic.get("diagnostic_id"),
                    "orphaned_pids": diagnostic.get("orphaned_pids", []),
                })
        return {
            "backend_rss_mb": metrics["backend_rss_mb"],
            "chrome_memory_mb": metrics["chrome_memory_mb"],
            "chromedriver_memory_mb": metrics["chromedriver_memory_mb"],
            "youtube_memory_mb": metrics["youtube_memory_mb"],
            "managed_browser_memory_mb": metrics["worker_memory_mb"],
            "active_browsers": state.get("total_active_browsers", state["count"]),
            "cleanup_failures": cleanup_failures,
            "potential_leak_detected": bool(cleanup_failures),
            "resource_sampling_seconds": 2,
            "resource_writes": "buffered",
        }

    @staticmethod
    def _alerts(ram: float, available_ram_gb: float, sustained_cpu: bool) -> list[dict[str, str]]:
        if ram >= RAM_SHUTDOWN_PERCENT:
            return [{
                "level": "danger",
                "message": f"RAM is {ram:.1f}%. Managed workers are stopping gracefully.",
            }]
        if sustained_cpu:
            return [{
                "level": "danger",
                "message": "Sustained CPU protection activated; managed workers are stopping.",
            }]
        if ram >= RAM_START_BLOCK_PERCENT:
            return [{
                "level": "warning",
                "message": f"RAM is {ram:.1f}%. New worker starts are blocked.",
            }]
        if available_ram_gb < MIN_AVAILABLE_RAM_GB:
            return [{
                "level": "warning",
                "message": f"Only {available_ram_gb:.2f} GB RAM is available. Starts are blocked.",
            }]
        return [{"level": "safe", "message": "System resources are within safe limits."}]
