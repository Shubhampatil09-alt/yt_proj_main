from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import RAM_SHUTDOWN_PERCENT
from app.main import create_app
from app.workers_model import WorkerStatus
from tests.conftest import FakeMetrics
from tests.test_phase6 import wait_for_status
from tests.test_youtube import CHANNEL_ID, VIDEO_ID, StubYouTube


class PlayerDriver:
    def __init__(self):
        self.service = SimpleNamespace(process=None)
        self.url = None
        self.ended = False
        self.error = None
        self.quit_called = False
        self.scripts = []

    def get(self, url):
        self.url = url

    def execute_script(self, script):
        self.scripts.append(script)
        return {"ready": True, "time": 49 if self.ended else 2,
                "duration": 49, "ended": self.ended, "paused": False,
                "state": "completed" if self.ended else "playing", "error": self.error}

    def quit(self):
        self.quit_called = True


@pytest.fixture
def selected_app(tmp_path, monkeypatch):
    from app import youtube_worker
    drivers = []

    def factory(_):
        driver = PlayerDriver()
        drivers.append(driver)
        return driver

    monkeypatch.setattr(youtube_worker, "visible_driver", factory)
    metrics = FakeMetrics()
    app = create_app(tmp_path / "selected.db", metrics_provider=metrics, auto_start=False)
    app.state.youtube = StubYouTube()
    with TestClient(app) as client:
        yield app, client, drivers, metrics


def prepare(app, client):
    assert client.post('/api/youtube/selection', json={"channel_id": CHANNEL_ID, "video_id": VIDEO_ID}).status_code == 200
    return app.state.dashboard.workers.state()["bots"][0]["bot_id"]


def test_bot_requires_selection(selected_app):
    app, client, drivers, _ = selected_app
    bot = app.state.dashboard.workers.state()["bots"][0]
    response = client.post(f'/api/bots/{bot["bot_id"]}/start')
    assert response.status_code == 409
    assert "Select and save" in response.json()["detail"]
    assert not drivers


def test_start_bot_plays_selected_video_once_and_records_history(selected_app):
    app, client, drivers, _ = selected_app
    bot_id = prepare(app, client)
    # A legacy repeat preference must never repeat YouTube playback.
    client.patch(f'/api/bots/{bot_id}', json={"settings": {"playback_mode": "repeat"}})
    assert client.post(f'/api/bots/{bot_id}/start').status_code == 200
    worker = app.state.dashboard.workers._current[bot_id]
    wait_for_status(worker, {"Running"})
    assert drivers[0].url == f'http://127.0.0.1:8000/youtube-player?video_id={VIDEO_ID}'
    assert worker.snapshot()["video_id"] == VIDEO_ID
    assert worker.ALLOW_REPEAT is False
    client.patch(f'/api/bots/{bot_id}', json={"settings": {"playback_mode": "repeat"}})
    drivers[0].ended = True
    worker.join(3)
    assert worker.status == WorkerStatus.STOPPED
    assert worker.snapshot()["playback_state"] == "completed"
    assert worker.snapshot()["progress_percent"] == 100
    assert drivers[0].quit_called
    assert len(drivers) == 1
    assert not any('testVideo' in script for script in drivers[0].scripts)
    run = app.state.dashboard.history.get_run(worker.run_id)
    assert run["final_status"] == "completed"
    assert run["browser_process_info"]["video_id"] == VIDEO_ID


def test_bot_and_diagnostic_cannot_overlap_even_after_limit_increase(selected_app):
    app, client, drivers, _ = selected_app
    bot_id = prepare(app, client)
    client.post('/api/workers/increase-limit')
    second = client.post('/api/bots', json={"display_name": "Second"}).json()["bot_id"]
    assert client.post(f'/api/bots/{bot_id}/start').status_code == 200
    wait_for_status(app.state.dashboard.workers._current[bot_id], {"Running"})
    assert client.post(f'/api/bots/{second}/start').status_code == 409
    assert client.post('/api/youtube/diagnostic/start').status_code != 200
    assert client.post('/api/bots/start-all').json()["count"] == 1
    assert len(drivers) == 1
    assert client.post(f'/api/bots/{bot_id}/stop').status_code == 200
    app.state.dashboard.workers._current[bot_id].join(3)
    assert drivers[0].quit_called


def test_diagnostic_reservation_blocks_bot(selected_app):
    app, client, drivers, _ = selected_app
    bot_id = prepare(app, client)
    manager = app.state.dashboard.workers
    manager.increase_limit()
    manager.reserve_external_slot("diagnostic-test")
    try:
        assert client.post(f'/api/bots/{bot_id}/start').status_code == 409
        assert not drivers
    finally:
        manager.release_external_slot("diagnostic-test")


def test_start_all_starts_only_one_and_safety_stops_it(selected_app):
    app, client, drivers, metrics = selected_app
    bot_id = prepare(app, client)
    client.post('/api/workers/increase-limit')
    client.post('/api/bots', json={"display_name": "Second"})
    assert client.post('/api/bots/start-all').json()["count"] == 1
    worker = app.state.dashboard.workers._current[bot_id]
    wait_for_status(worker, {"Running"})
    metrics.ram = RAM_SHUTDOWN_PERCENT
    client.get('/api/metrics')
    worker.join(3)
    assert worker.status == WorkerStatus.STOPPED
    assert drivers[0].quit_called
    assert len(drivers) == 1
    assert app.state.dashboard.history.get_run(worker.run_id)["final_status"] == "safety_stopped"


def test_player_error_is_reported_for_bot(selected_app):
    app, client, drivers, _ = selected_app
    bot_id = prepare(app, client)
    client.post(f'/api/bots/{bot_id}/start')
    worker = app.state.dashboard.workers._current[bot_id]
    wait_for_status(worker, {"Running"})
    drivers[0].error = "YouTube error 153"
    worker.join(3)
    assert worker.status == WorkerStatus.FAILED
    assert "153" in worker.snapshot()["last_error"]
    assert drivers[0].quit_called
