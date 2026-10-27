import pytest
from fastapi.testclient import TestClient

from app.config import CPU_SUSTAINED_SAMPLES, RAM_SHUTDOWN_PERCENT
from app.main import create_app
from app.youtube_diagnostic import YouTubeDiagnosticManager
from tests.conftest import FakeMetrics, FakeWorker
from tests.test_phase6 import RunningDiagnosticDriver, wait_for_status
from tests.test_youtube import CHANNEL_ID, VIDEO_ID, StubYouTube


def setup_watch(tmp_path):
    metrics = FakeMetrics()
    app = create_app(tmp_path / "watch.db", metrics_provider=metrics,
                     worker_factory=lambda worker_id: FakeWorker(worker_id), auto_start=False)
    app.state.youtube = StubYouTube()
    drivers = []

    class Driver(RunningDiagnosticDriver):
        def get(self, url):
            self.url = url
            super().get(url)

    def factory(_):
        driver = Driver()
        drivers.append(driver)
        return driver

    manager = app.state.dashboard.workers
    diagnostic = YouTubeDiagnosticManager(app.state.dashboard.history, manager, factory)
    manager.register_external_controller(diagnostic)
    app.state.youtube_diagnostic = diagnostic
    app.state.dashboard.youtube_diagnostic = diagnostic
    return app, metrics, diagnostic, drivers


def select(client):
    assert client.post("/api/youtube/selection", json={"channel_id": CHANNEL_ID, "video_id": VIDEO_ID}).status_code == 200


def test_watch_saved_selection_stop_and_analytics(tmp_path):
    app, _, diagnostic, drivers = setup_watch(tmp_path)
    with TestClient(app) as client:
        select(client)
        assert client.post("/api/youtube/diagnostic/start").status_code == 200
        wait_for_status(diagnostic, {"Running"})
        assert drivers[0].url == f"http://127.0.0.1:8000/youtube-player?video_id={VIDEO_ID}"
        assert client.post("/api/youtube/diagnostic/start").status_code != 200
        state = client.post("/api/bots/start-all").json()
        assert state["running_bots"] == 0
        assert state["total_active_browsers"] == 1
        assert len(drivers) == 1
        assert client.post("/api/youtube/diagnostic/stop").status_code == 200
        diagnostic.join(3)
        assert drivers[0].quit_called
        records = client.get("/api/analytics/youtube-diagnostics").json()["items"]
        assert len(records) == 1
        assert records[0]["video_id"] == VIDEO_ID
        assert records[0]["status"] == "Stopped"
        assert records[0]["cleanup_verified"] is True
        assert "YouTube diagnostic history" in client.get("/analytics").text
        assert "Watch Selected Video" in client.get("/").text
        assert len(drivers) == 1


@pytest.mark.parametrize("trigger", ["ram", "cpu"])
def test_watch_resource_shutdown(tmp_path, trigger):
    app, metrics, diagnostic, drivers = setup_watch(tmp_path)
    with TestClient(app) as client:
        select(client)
        client.post("/api/youtube/diagnostic/start")
        wait_for_status(diagnostic, {"Running"})
        if trigger == "ram":
            metrics.ram = RAM_SHUTDOWN_PERCENT
        else:
            metrics.cpu = 100
        for _ in range(CPU_SUSTAINED_SAMPLES):
            client.get("/api/metrics")
        diagnostic.join(3)
        assert diagnostic.snapshot()["status"] == "Safety Stopped"
        assert drivers[0].quit_called
        assert app.state.dashboard.workers.state()["external_active"] == 0
        assert client.post("/api/youtube/diagnostic/start").status_code != 200


def test_watch_uses_visible_chrome(monkeypatch, tmp_path):
    from app import youtube_diagnostic
    calls = []
    monkeypatch.setattr(youtube_diagnostic, "chrome_driver_factory", lambda path, **kwargs: calls.append(kwargs))
    youtube_diagnostic.diagnostic_driver_factory(tmp_path)
    assert calls == [{"headless": False}]


def test_watch_does_not_stop_after_fifteen_seconds(tmp_path):
    app, _, diagnostic, drivers = setup_watch(tmp_path)
    with TestClient(app) as client:
        select(client)
        client.post("/api/youtube/diagnostic/start")
        wait_for_status(diagnostic, {"Running"})
        diagnostic._started_monotonic -= 60
        diagnostic.join(0.8)
        assert diagnostic.snapshot()["elapsed_seconds"] >= 60
        assert diagnostic.snapshot()["status"] == "Running"
        assert diagnostic.snapshot()["maximum_seconds"] is None
        assert not drivers[0].quit_called
        client.post("/api/youtube/diagnostic/stop")
        diagnostic.join(3)
        assert drivers[0].quit_called


def test_local_player_referrer_and_video_validation(tmp_path):
    app, _, _, _ = setup_watch(tmp_path)
    with TestClient(app) as client:
        response = client.get(f"/youtube-player?video_id={VIDEO_ID}")
        assert response.status_code == 200
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert f'data-video-id="{VIDEO_ID}"' in response.text
        assert "test-video.mp4" not in response.text
        assert client.get('/youtube-player?video_id=invalid').status_code == 422


def test_iframe_error_is_reported_and_browser_closed(tmp_path):
    app, _, diagnostic, drivers = setup_watch(tmp_path)
    with TestClient(app) as client:
        select(client)
        original_factory = diagnostic.driver_factory

        def failing_factory(path):
            driver = original_factory(path)
            driver.execute_script = lambda script: {"ready": False, "error": "YouTube error 153: Missing HTTP Referer"}
            return driver

        diagnostic.driver_factory = failing_factory
        assert client.post("/api/youtube/diagnostic/start").status_code == 200
        diagnostic.join(3)
        assert diagnostic.snapshot()["status"] == "Failed"
        assert drivers[0].quit_called
        record = client.get("/api/analytics/youtube-diagnostics").json()["items"][0]
        assert "153" in record["error_message"]
        assert record["cleanup_verified"] is True
