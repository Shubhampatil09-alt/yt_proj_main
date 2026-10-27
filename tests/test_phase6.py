import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.database import JobHistory
from app.config import RAM_START_BLOCK_PERCENT, read_youtube_api_key
from app.main import create_app
from app.workers import WorkerManager
from app.youtube import YouTubeDataClient
from app.youtube_diagnostic import YouTubeDiagnosticManager
from tests.conftest import FakeMetrics, FakeWorker
from tests.test_youtube import (
    CHANNEL_ID,
    VIDEO_ID,
    FakeResponse,
    RoutedHTTP,
    StubDiagnostic,
    StubYouTube,
    channel_item,
    video_item,
)


class RunningDiagnosticDriver:
    def __init__(self):
        self.service = SimpleNamespace(process=None)
        self.quit_called = False

    def get(self, url):
        assert url.startswith("http://127.0.0.1:8000/youtube-player?video_id=")

    def execute_script(self, script):
        if "? window.youtubeDiagnostic" in script:
            return {"ready": True, "error": None}
        if "return v.play" in script:
            return None
        return {"time": 2.5, "paused": False, "ended": False, "error": None}

    def quit(self):
        self.quit_called = True


def wait_for_status(diagnostic, expected, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if diagnostic.snapshot()["status"] in expected:
            return diagnostic.snapshot()
        time.sleep(0.01)
    raise AssertionError(f"Diagnostic did not reach {expected}: {diagnostic.snapshot()}")


def test_process_youtube_key_takes_precedence(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "  process-key-value  ")
    assert read_youtube_api_key() == "process-key-value"


def test_dashboard_contains_complete_selection_and_diagnostic_workflow(tmp_path):
    app = create_app(
        tmp_path / "workflow.db",
        metrics_provider=FakeMetrics(),
        worker_factory=lambda worker_id: FakeWorker(worker_id),
        auto_start=False,
    )
    with TestClient(app) as client:
        page = client.get("/")

    assert page.status_code == 200
    for label in ("Search Channel", "Select Video", "Run Diagnostic", "View Results"):
        assert label in page.text
    assert "/static/css/dashboard.css?v=6.0.12" in page.text
    assert "/static/js/dashboard.js?v=6.0.12" in page.text
    for element_id in (
        'id="selectedVideo"', 'id="changeVideo"', 'id="startDiagnostic"',
        'id="diagnosticReason"', 'id="diagnosticResults"', 'id="loadMoreVideos"',
    ):
        assert element_id in page.text

    script = (Path(__file__).parents[1] / "static" / "js" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    assert "async function changeVideo()" in script
    assert "await loadVideos(selection.channel_id)" in script
    assert '$("channelQuery").value = selection.channel_title || selection.channel_id' in script
    assert "renderDiagnosticResults" in script
    assert "eligibility.reason" in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert "refreshInFlight" in script
    assert '"Warning"' in script
    assert '"Instruction"' in script

    styles = (Path(__file__).parents[1] / "static" / "css" / "dashboard.css").read_text(
        encoding="utf-8"
    )
    assert "grid-template-columns: repeat(3,minmax(0,1fr))" in styles
    assert ".diagnostic-cta > button" in styles
    assert ".metric-card { min-height: 68px" in styles
    assert ".metric-label { margin-bottom: 3px; color: #9ca7bc; font-size: 12px" in styles
    assert ".metric-card small { display: block; margin-top: 4px; color: #96a1b5; font-size: 11px" in styles


def test_resource_guardrails_are_rendered_in_cpu_and_ram_panels(tmp_path):
    app = create_app(
        tmp_path / "resource-panels.db",
        metrics_provider=FakeMetrics(),
        worker_factory=lambda worker_id: FakeWorker(worker_id),
        auto_start=False,
    )
    with TestClient(app) as client:
        page = client.get("/")

    assert page.status_code == 200
    for element_id in (
        'id="cpuSafetyAlert"', 'id="cpuThreshold"', 'id="cpuSampleProgress"',
        'id="ramSafetyAlert"', 'id="ramStartThreshold"',
        'id="ramShutdownThreshold"', 'id="minimumAvailableRam"',
        'id="systemRamBreakdown"', 'id="backendRamBreakdown"',
        'id="chromeRamBreakdown"', 'id="driverRamBreakdown"',
    ):
        assert element_id in page.text
    assert "Safety alerts" not in page.text

    script = (Path(__file__).parents[1] / "static" / "js" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    assert "function renderResourceSafety(data)" in script
    assert "thresholds.ram_start_block" in script
    assert "thresholds.cpu_sustained" in script


def test_channel_video_pagination_preserves_next_page_token():
    http = RoutedHTTP({
        "channels": FakeResponse({"items": [channel_item()]}),
        "playlistItems": lambda params: FakeResponse({
            "items": [{"contentDetails": {"videoId": VIDEO_ID}}],
            "nextPageToken": None if params.get("pageToken") else "NEXT_PAGE",
        }),
        "videos": FakeResponse({"items": [video_item()]}),
    })
    youtube = YouTubeDataClient("server-only-key", http)

    first = youtube.channel_videos_page(CHANNEL_ID)
    second = youtube.channel_videos_page(CHANNEL_ID, first["next_page_token"])

    assert first["next_page_token"] == "NEXT_PAGE"
    assert second["next_page_token"] is None
    playlist_calls = [params for resource, params in http.calls if resource == "playlistItems"]
    assert "pageToken" not in playlist_calls[0]
    assert playlist_calls[1]["pageToken"] == "NEXT_PAGE"


def test_saved_selection_drives_snapshot_and_survives_refresh(tmp_path):
    database = tmp_path / "selection-refresh.db"
    app = create_app(
        database, metrics_provider=FakeMetrics(),
        worker_factory=lambda worker_id: FakeWorker(worker_id), auto_start=False,
    )
    app.state.youtube = StubYouTube()
    app.state.youtube_diagnostic = StubDiagnostic()
    with TestClient(app) as client:
        client.post(
            "/api/youtube/selection",
            json={"channel_id": CHANNEL_ID, "video_id": VIDEO_ID},
        )
        snapshot = client.get("/api/dashboard/snapshot").json()["youtube"]

    assert snapshot["selection"]["video_id"] == VIDEO_ID
    assert snapshot["selection"]["video_url"].endswith(VIDEO_ID)
    assert "eligibility" in snapshot


def test_manual_stop_persists_diagnostic_result_and_cleanup(tmp_path):
    history = JobHistory(tmp_path / "manual-stop.db")
    history.initialize()
    manager = WorkerManager(
        history, target_workers=0,
        worker_factory=lambda worker_id: FakeWorker(worker_id),
        metrics_provider=FakeMetrics(), session_id="session",
    )
    driver = RunningDiagnosticDriver()
    diagnostic = YouTubeDiagnosticManager(history, manager, lambda _: driver)
    manager.register_external_controller(diagnostic)

    diagnostic.start({"video_id": VIDEO_ID, "embeddable": True})
    wait_for_status(diagnostic, {"Running"})
    assert diagnostic.stop("user_stop") is True
    assert diagnostic.stop("user_stop") is False
    diagnostic.join(3)

    state = diagnostic.snapshot()
    stored = history.latest_youtube_diagnostic(VIDEO_ID)
    assert state["status"] == "Stopped"
    assert state["browser_status"] == "closed"
    assert state["cleanup_verified"] is True
    assert driver.quit_called is True
    assert stored["status"] == "Stopped"
    assert stored["stop_reason"] == "user_stop"
    assert stored["playback_status"] == "stop requested"
    assert stored["cleanup_verified"] is True


def test_safety_shutdown_marks_manual_diagnostic_safety_stopped(tmp_path):
    history = JobHistory(tmp_path / "safety-stop.db")
    history.initialize()
    manager = WorkerManager(
        history, target_workers=0,
        worker_factory=lambda worker_id: FakeWorker(worker_id),
        metrics_provider=FakeMetrics(), session_id="session",
    )
    diagnostic = YouTubeDiagnosticManager(
        history, manager, lambda _: RunningDiagnosticDriver()
    )
    manager.register_external_controller(diagnostic)

    diagnostic.start({"video_id": VIDEO_ID, "embeddable": True})
    wait_for_status(diagnostic, {"Running"})
    manager.update_safety("RAM reached 80%", stop_workers=True, latched=True)
    diagnostic.join(3)

    assert diagnostic.snapshot()["status"] == "Safety Stopped"
    assert history.latest_youtube_diagnostic(VIDEO_ID)["status"] == "Safety Stopped"
    assert manager.state()["external_active"] == 0


def test_snapshot_explains_resource_block_for_diagnostic(tmp_path):
    app = create_app(
        tmp_path / "resource-reason.db", metrics_provider=FakeMetrics(ram=RAM_START_BLOCK_PERCENT),
        worker_factory=lambda worker_id: FakeWorker(worker_id), auto_start=False,
    )
    app.state.youtube = StubYouTube()
    app.state.youtube_diagnostic = StubDiagnostic()
    with TestClient(app) as client:
        client.post(
            "/api/youtube/selection",
            json={"channel_id": CHANNEL_ID, "video_id": VIDEO_ID},
        )
        youtube = client.get("/api/dashboard/snapshot").json()["youtube"]

    assert youtube["eligibility"]["allowed"] is False
    assert "RAM" in youtube["eligibility"]["reason"]


def test_snapshot_explains_when_a_diagnostic_is_already_running(tmp_path):
    class RunningStubDiagnostic(StubDiagnostic):
        def snapshot(self):
            return {
                "status": "Running", "elapsed_seconds": 3,
                "playback_seconds": 2, "process": {"memory_mb": 25},
            }

    app = create_app(
        tmp_path / "running-reason.db", metrics_provider=FakeMetrics(),
        worker_factory=lambda worker_id: FakeWorker(worker_id), auto_start=False,
    )
    app.state.youtube = StubYouTube()
    app.state.youtube_diagnostic = RunningStubDiagnostic()
    with TestClient(app) as client:
        client.post(
            "/api/youtube/selection",
            json={"channel_id": CHANNEL_ID, "video_id": VIDEO_ID},
        )
        eligibility = client.get("/api/dashboard/snapshot").json()["youtube"]["eligibility"]

    assert eligibility["allowed"] is False
    assert "already running" in eligibility["reason"]
