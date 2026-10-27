import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.database import JobHistory
from app.config import MIN_AVAILABLE_RAM_GB
from app.main import create_app
from app.workers import WorkerManager
from app.workers_model import WorkerStatus, utc_time
from tests.conftest import FakeMetrics, FakeWorker


def make_manager(tmp_path, factory=None, metrics=None):
    store = JobHistory(tmp_path / "phase4.db")
    store.initialize()
    store.seed_default_profiles(2, utc_time())
    store.start_session("phase4-session", utc_time())
    return store, WorkerManager(
        store, worker_factory=factory or (lambda worker_id: FakeWorker(worker_id)),
        metrics_provider=metrics or FakeMetrics(), session_id="phase4-session",
    )


def test_profiles_persist_and_restore_as_stopped(tmp_path):
    database = tmp_path / "persistent.db"
    first = create_app(database, metrics_provider=FakeMetrics(),
                       worker_factory=lambda worker_id: FakeWorker(worker_id), auto_start=False)
    with TestClient(first) as client:
        created = client.post("/api/bots", json={"display_name": "Living Room", "settings": {"loop": False}})
        assert created.status_code == 201
        bot_id = created.json()["bot_id"]
        client.post(f"/api/bots/{bot_id}/start")

    second = create_app(database, metrics_provider=FakeMetrics(),
                        worker_factory=lambda worker_id: FakeWorker(worker_id), auto_start=False)
    with TestClient(second) as client:
        bot = next(item for item in client.get("/api/bots").json()["bots"] if item["bot_id"] == bot_id)
        assert bot["display_name"] == "Living Room"
        assert bot["settings"] == {"loop": False}
        assert bot["status"] == "Stopped"
        assert client.get("/api/bots").json()["count"] == 0


def test_individual_start_stop_restart_uses_fresh_workers(tmp_path):
    registry = {}
    def factory(worker_id):
        registry[worker_id] = FakeWorker(worker_id)
        return registry[worker_id]
    _, manager = make_manager(tmp_path, factory)
    bot_id = manager.state()["bots"][0]["bot_id"]

    first = manager.start_bot(bot_id)
    first_id = next(bot for bot in first["bots"] if bot["bot_id"] == bot_id)["worker_id"]
    manager.stop_bot(bot_id)
    restarted = manager.restart_bot(bot_id)
    second_id = next(bot for bot in restarted["bots"] if bot["bot_id"] == bot_id)["worker_id"]

    assert second_id > first_id
    assert registry[first_id].stop_calls == 1
    assert next(bot for bot in restarted["bots"] if bot["bot_id"] == bot_id)["status"] == "Running"


def test_removal_requires_confirmation_and_stopped_state(tmp_path):
    _, manager = make_manager(tmp_path)
    bot_id = manager.state()["bots"][0]["bot_id"]
    manager.start_bot(bot_id)
    with pytest.raises(ValueError, match="fully stopped"):
        manager.remove_bot(bot_id, confirmed=True)
    manager.stop_bot(bot_id)
    with pytest.raises(ValueError, match="confirmation"):
        manager.remove_bot(bot_id)
    state = manager.remove_bot(bot_id, confirmed=True)
    assert all(bot["bot_id"] != bot_id for bot in state["bots"])


def test_removing_profile_preserves_associated_history(tmp_path):
    store, manager = make_manager(tmp_path)
    bot_id = manager.state()["bots"][0]["bot_id"]
    name = manager.state()["bots"][0]["display_name"]
    store.begin_run("preserved", "phase4-session", 41, utc_time(), bot_id, name)
    store.finalize_run(
        "preserved", ended_at=utc_time(), execution_duration=3, final_status="completed",
        playback_duration=3, playback_progress=100, error_message=None,
        browser_process_info={}, stop_reason=None,
    )
    manager.remove_bot(bot_id, confirmed=True)

    run = store.get_run("preserved")
    assert run["bot_id"] == bot_id
    assert run["bot_name"] == name


def test_concurrent_start_requests_only_create_one_execution(tmp_path):
    _, manager = make_manager(tmp_path)
    bot_id = manager.state()["bots"][0]["bot_id"]
    def start():
        try:
            manager.start_bot(bot_id)
            return "started"
        except ValueError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: start(), range(8)))
    assert results.count("started") == 1
    assert manager.state()["count"] == 1
    assert manager.state()["total_records"] == 1


def test_available_ram_floor_blocks_every_start_path(tmp_path):
    metrics = FakeMetrics(ram=69)
    metrics.virtual_memory = lambda: type("Memory", (), {
        "total": 8 * 1024**3, "available": int((MIN_AVAILABLE_RAM_GB - 0.1) * 1024**3), "percent": 69.0
    })()
    _, manager = make_manager(tmp_path, metrics=metrics)
    bot_id = manager.state()["bots"][0]["bot_id"]
    with pytest.raises(ValueError, match=f"at least {MIN_AVAILABLE_RAM_GB:.2f} GB"):
        manager.start_bot(bot_id)
    with pytest.raises(ValueError, match=f"at least {MIN_AVAILABLE_RAM_GB:.2f} GB"):
        manager.restart_bot(bot_id)
    with pytest.raises(ValueError, match=f"at least {MIN_AVAILABLE_RAM_GB:.2f} GB"):
        manager.start_all()


def test_failed_bot_can_restart_with_a_fresh_execution(tmp_path):
    calls = []
    class FailOnceWorker(FakeWorker):
        def start(self):
            calls.append(self.id)
            if len(calls) == 1:
                self.status = WorkerStatus.FAILED
                return True
            return super().start()
    _, manager = make_manager(tmp_path, factory=lambda worker_id: FailOnceWorker(worker_id))
    bot_id = manager.state()["bots"][0]["bot_id"]
    failed = manager.start_bot(bot_id)
    assert next(bot for bot in failed["bots"] if bot["bot_id"] == bot_id)["status"] == "Failed"

    restarted = manager.restart_bot(bot_id)
    bot = next(item for item in restarted["bots"] if item["bot_id"] == bot_id)
    assert bot["status"] == "Running"
    assert bot["worker_id"] == 2


def test_shutdown_stops_workers_and_retains_profiles(tmp_path):
    registry = {}
    def factory(worker_id):
        registry[worker_id] = FakeWorker(worker_id)
        return registry[worker_id]
    store, manager = make_manager(tmp_path, factory)
    manager.start_all()
    manager.shutdown()
    assert all(worker.stop_calls == 1 for worker in registry.values())
    assert len(store.list_bot_profiles()) == 2
    assert all(profile["last_status"] == "Stopped" for profile in store.list_bot_profiles())


def test_phase3_database_migrates_without_losing_old_runs(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE sessions(session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, app_version TEXT NOT NULL DEFAULT '3.0.0');
        CREATE TABLE test_runs(run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, worker_id INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, execution_duration REAL NOT NULL DEFAULT 0, final_status TEXT, playback_duration REAL NOT NULL DEFAULT 0, playback_progress REAL NOT NULL DEFAULT 0, error_message TEXT, browser_process_info TEXT NOT NULL DEFAULT '{}', stop_reason TEXT);
        INSERT INTO sessions(session_id,started_at) VALUES('old','2025-01-01T00:00:00+00:00');
        INSERT INTO test_runs(run_id,session_id,worker_id,started_at,final_status) VALUES('old-run','old',1,'2025-01-01T00:00:00+00:00','completed');
    """)
    connection.close()
    store = JobHistory(path)
    store.initialize()
    run = store.get_run("old-run")
    assert run["bot_id"] is None and run["bot_name"] is None
    assert store.list_bot_profiles() == []
