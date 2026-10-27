from collections import namedtuple
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.workers_model import WorkerStatus


Memory = namedtuple("Memory", "total available percent")


class FakeMetrics:
    def __init__(self, cpu: float = 24.0, ram: float = 45.0) -> None:
        self.cpu = cpu
        self.ram = ram

    def cpu_percent(self, interval=None) -> float:
        return self.cpu

    def virtual_memory(self) -> Memory:
        total = 8 * 1024**3
        available = total * (1 - self.ram / 100)
        return Memory(total=total, available=available, percent=self.ram)


class FakeWorker:
    def __init__(self, worker_id: int) -> None:
        self.id = worker_id
        self.status = WorkerStatus.STOPPED
        self.stop_calls = 0
        self.started = Event()

    def start(self) -> bool:
        self.status = WorkerStatus.RUNNING
        self.started.set()
        return True

    def stop(self, reason="user_stop") -> bool:
        if self.status in {WorkerStatus.STOPPED, WorkerStatus.FAILED, WorkerStatus.STOPPING}:
            return False
        self.stop_calls += 1
        self.stop_reason = reason
        self.status = WorkerStatus.STOPPED
        return True

    def join(self, timeout=None) -> None:
        return None

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "status": self.status.value,
            "playback_state": "playing" if self.status == WorkerStatus.RUNNING else "stopped",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": None,
            "elapsed_seconds": 1.0,
            "progress_percent": 25.0,
            "current_time": 1.0,
            "duration": 4.0,
            "last_error": None,
            "process": {"driver_pid": 1000 + self.id, "pids": [1000 + self.id], "memory_mb": 50.0},
        }


@pytest.fixture
def fake_metrics():
    return FakeMetrics()


@pytest.fixture
def worker_registry():
    return {}


@pytest.fixture
def client(tmp_path, fake_metrics, worker_registry):
    def factory(worker_id):
        worker = FakeWorker(worker_id)
        worker_registry[worker_id] = worker
        return worker

    app = create_app(
        tmp_path / "test.db",
        metrics_provider=fake_metrics,
        worker_factory=factory,
        auto_start=False,
    )
    app.state.dashboard.workers.start_all()
    with TestClient(app) as test_client:
        yield test_client
