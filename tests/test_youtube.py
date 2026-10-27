from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.database import JobHistory
from app.main import create_app
from app.workers import WorkerManager
from app.workers_model import utc_time
from app.youtube import YouTubeAPIError, YouTubeDataClient, parse_channel_query
from app.youtube_diagnostic import YouTubeDiagnosticManager
from app.config import RAM_START_BLOCK_PERCENT
from tests.conftest import FakeMetrics, FakeWorker


CHANNEL_ID = "UC" + "a" * 22
OTHER_CHANNEL_ID = "UC" + "b" * 22
VIDEO_ID = "AbCdEf123_-"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
    def json(self):
        return self.payload


class RoutedHTTP:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
    def get(self, url, params):
        self.calls.append((url.rsplit("/", 1)[-1], dict(params)))
        response = self.routes[url.rsplit("/", 1)[-1]]
        return response(dict(params)) if callable(response) else response
    def close(self):
        pass


def channel_item():
    return {"id": CHANNEL_ID, "snippet": {"title": "Test Channel", "thumbnails": {"default": {"url": "https://img.test/channel.jpg"}}}, "statistics": {"subscriberCount": "1200", "videoCount": "4"}, "contentDetails": {"relatedPlaylists": {"uploads": "UUuploads"}}}


def video_item(channel_id=CHANNEL_ID):
    return {"id": VIDEO_ID, "snippet": {"channelId": channel_id, "channelTitle": "Test Channel", "title": "Diagnostic Video", "publishedAt": "2026-01-01T00:00:00Z", "thumbnails": {"medium": {"url": "https://img.test/video.jpg"}}}, "contentDetails": {"duration": "PT1M5S"}, "statistics": {"viewCount": "123", "likeCount": "10"}, "status": {"privacyStatus": "public", "embeddable": True}}


def test_channel_query_supports_name_handle_id_and_url():
    assert parse_channel_query("Example Channel") == ("name", "Example Channel")
    assert parse_channel_query("@example") == ("handle", "example")
    assert parse_channel_query(CHANNEL_ID) == ("id", CHANNEL_ID)
    assert parse_channel_query(f"https://www.youtube.com/channel/{CHANNEL_ID}") == ("id", CHANNEL_ID)
    assert parse_channel_query("https://youtube.com/@example") == ("handle", "example")
    with pytest.raises(ValueError):
        parse_channel_query("https://example.com/channel/nope")


def test_official_api_channel_search_video_listing_and_statistics():
    http = RoutedHTTP({
        "channels": lambda params: FakeResponse({"items": [channel_item()] if params.get("id") or params.get("forHandle") or params.get("part") == "contentDetails" else []}),
        "playlistItems": FakeResponse({"items": [{"contentDetails": {"videoId": VIDEO_ID}}]}),
        "videos": FakeResponse({"items": [video_item()]}),
    })
    youtube = YouTubeDataClient("secret-key", http)

    channels = youtube.search_channels("@example")
    videos = youtube.channel_videos(CHANNEL_ID)

    assert channels[0]["channel_id"] == CHANNEL_ID
    assert channels[0]["subscriber_count"] == 1200
    assert videos[0]["duration_seconds"] == 65
    assert videos[0]["statistics"]["viewCount"] == 123
    assert all(call[1]["key"] == "secret-key" for call in http.calls)


def test_quota_error_is_safe_and_does_not_expose_api_key():
    http = RoutedHTTP({"search": FakeResponse({"error": {"errors": [{"reason": "quotaExceeded"}]}}, 403)})
    youtube = YouTubeDataClient("top-secret-key", http)
    with pytest.raises(YouTubeAPIError) as caught:
        youtube.search_channels("test")
    assert caught.value.code == "quota_exceeded"
    assert caught.value.status_code == 429
    assert "top-secret-key" not in str(caught.value)


def test_quota_error_maps_to_safe_http_response(client):
    class QuotaYouTube:
        configured = True
        def search_channels(self, query):
            raise YouTubeAPIError("YouTube API quota is exhausted.", "quota_exceeded", 429)
    client.app.state.youtube = QuotaYouTube()
    response = client.get("/api/youtube/channels", params={"q": "test"})
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "quota_exceeded"
    assert "key" not in response.text.lower()


class StubYouTube:
    configured = True
    def search_channels(self, query):
        return [{"channel_id": CHANNEL_ID, "title": "Test Channel"}]
    def channel_videos(self, channel_id):
        return [self.verify_video(channel_id, VIDEO_ID)]
    def verify_video(self, channel_id, video_id):
        if video_id != VIDEO_ID:
            raise YouTubeAPIError("The selected video does not belong to the selected channel.", "channel_mismatch", 409)
        item = video_item(channel_id)
        return YouTubeDataClient._video(item)


class StubDiagnostic:
    def __init__(self):
        self.starts = 0
    def snapshot(self):
        return {"status": "Idle", "playback_seconds": 0, "process": {"memory_mb": 0}}
    def start(self, video):
        self.starts += 1
        return {"status": "Starting", "video_id": video["video_id"], "process": {"memory_mb": 0}}
    def stop(self, reason="user_stop"):
        return True


def test_selection_is_verified_persisted_and_exactly_one(tmp_path):
    database = tmp_path / "youtube.db"
    app = create_app(database, metrics_provider=FakeMetrics(),
                     worker_factory=lambda worker_id: FakeWorker(worker_id), auto_start=False)
    app.state.youtube = StubYouTube()
    app.state.youtube_diagnostic = StubDiagnostic()
    with TestClient(app) as client:
        saved = client.post("/api/youtube/selection", json={"channel_id": CHANNEL_ID, "video_id": VIDEO_ID})
        rejected = client.post("/api/youtube/selection", json={"channel_id": CHANNEL_ID, "video_id": "12345678901"})
        selection = client.get("/api/youtube/selection").json()["selection"]
        assert saved.status_code == 200
        assert rejected.status_code == 409
        assert selection["video_id"] == VIDEO_ID
        assert selection["channel_id"] == CHANNEL_ID
        assert selection["statistics"]["viewCount"] == 123

    restarted = create_app(database, metrics_provider=FakeMetrics(),
                           worker_factory=lambda worker_id: FakeWorker(worker_id), auto_start=False)
    with TestClient(restarted) as client:
        assert client.get("/api/youtube/selection").json()["selection"]["video_id"] == VIDEO_ID


def test_diagnostic_requires_selection_and_never_starts_from_start_all(tmp_path):
    app = create_app(tmp_path / "restriction.db", metrics_provider=FakeMetrics(),
                     worker_factory=lambda worker_id: FakeWorker(worker_id), auto_start=False)
    app.state.youtube = StubYouTube()
    diagnostic = StubDiagnostic()
    app.state.youtube_diagnostic = diagnostic
    with TestClient(app) as client:
        assert client.post("/api/youtube/diagnostic/start").status_code == 409
        client.post("/api/bots/start-all")
        assert diagnostic.starts == 0
        client.post("/api/youtube/selection", json={"channel_id": CHANNEL_ID, "video_id": VIDEO_ID})
        assert client.post("/api/youtube/diagnostic/start").status_code == 200
        assert diagnostic.starts == 1


class DiagnosticDriver:
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
        return {"time": 1, "paused": False, "ended": True, "error": None}
    def quit(self):
        self.quit_called = True


def test_manual_diagnostic_uses_one_slot_and_cleans_up(tmp_path):
    history = JobHistory(tmp_path / "diagnostic.db")
    history.initialize()
    history.seed_default_profiles(2, utc_time())
    manager = WorkerManager(history, worker_factory=lambda worker_id: FakeWorker(worker_id),
                            metrics_provider=FakeMetrics(), session_id="session")
    driver = DiagnosticDriver()
    diagnostic = YouTubeDiagnosticManager(history, manager, lambda _: driver)
    manager.register_external_controller(diagnostic)

    diagnostic.start({"video_id": VIDEO_ID, "embeddable": True})
    diagnostic.join(3)

    assert diagnostic.snapshot()["status"] == "Completed"
    assert driver.quit_called is True
    assert manager.state()["external_active"] == 0
    assert manager.state()["total_active_browsers"] == 0


def test_manual_diagnostic_obeys_the_same_resource_gate(tmp_path):
    history = JobHistory(tmp_path / "blocked-diagnostic.db")
    history.initialize()
    metrics = FakeMetrics(ram=RAM_START_BLOCK_PERCENT)
    manager = WorkerManager(history, worker_factory=lambda worker_id: FakeWorker(worker_id),
                            metrics_provider=metrics, session_id="session")
    diagnostic = YouTubeDiagnosticManager(history, manager, lambda _: DiagnosticDriver())
    with pytest.raises(ValueError, match="RAM"):
        diagnostic.start({"video_id": VIDEO_ID, "embeddable": True})
    assert manager.state()["external_active"] == 0


def test_consolidated_snapshot_and_performance_profile(client):
    snapshot = client.get("/api/dashboard/snapshot")
    performance = client.get("/api/performance")
    assert snapshot.status_code == 200
    assert set(snapshot.json()) == {"metrics", "bots", "logs", "youtube"}
    assert "configured" in snapshot.json()["youtube"]
    assert performance.status_code == 200
    assert performance.json()["potential_leak_detected"] is False
    assert "backend_rss_mb" in performance.json()
    assert "chromedriver_memory_mb" in performance.json()
