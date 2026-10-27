import pytest

from app.config import BOT_RAM_ESTIMATE_MB
from app.workers_model import WorkerStatus
from tests.conftest import FakeMetrics, FakeWorker
from tests.test_phase4 import make_manager


def test_start_is_blocked_when_projected_ram_would_exceed_the_limit(tmp_path):
    # 80% is below the 84% start limit, but one more ~740 MB browser would cross it.
    _, manager = make_manager(tmp_path, metrics=FakeMetrics(ram=80))
    bot_id = manager.state()["bots"][0]["bot_id"]

    with pytest.raises(ValueError, match="Memory limit reached"):
        manager.start_bot(bot_id)
    capacity = manager.state()["capacity"]
    assert capacity["memory_bots"] == 0
    assert capacity["memory_limited"] is True
    assert "can't start more bots" in capacity["message"]


def test_capacity_reports_remaining_bots_and_concurrency_limit(tmp_path):
    _, manager = make_manager(tmp_path, metrics=FakeMetrics(ram=45))
    capacity = manager.state()["capacity"]
    # 8 GB at 45% leaves room for four ~740 MB bots under the 84% limit.
    assert capacity["memory_bots"] == 4
    assert capacity["more_bots"] == 1
    assert capacity["message"] == "You can run about 1 more bot."

    manager.start_bot(manager.state()["bots"][0]["bot_id"])
    capacity = manager.state()["capacity"]
    assert capacity["more_bots"] == 0
    assert capacity["memory_limited"] is False
    assert "Concurrency limit" in capacity["message"]


def test_starting_browsers_reserve_their_estimated_ram(tmp_path):
    _, manager = make_manager(tmp_path, metrics=FakeMetrics(ram=45))
    manager.max_workers = 5
    before = manager.state()["capacity"]["memory_bots"]
    manager.start_bot(manager.state()["bots"][0]["bot_id"])
    # The fake browser reports 50 MB, so the rest of the estimate is held as pending.
    assert manager.state()["capacity"]["memory_bots"] == before - 1


def test_estimate_rises_to_the_observed_browser_peak(tmp_path):
    class HeavyWorker(FakeWorker):
        def snapshot(self):
            data = super().snapshot()
            data["process"]["memory_mb"] = BOT_RAM_ESTIMATE_MB + 500
            return data

    _, manager = make_manager(tmp_path, factory=lambda worker_id: HeavyWorker(worker_id))
    manager.start_bot(manager.state()["bots"][0]["bot_id"])
    assert manager.state()["capacity"]["estimate_mb"] == BOT_RAM_ESTIMATE_MB + 500
    assert manager.state()["bots"][0]["status"] == WorkerStatus.RUNNING.value
