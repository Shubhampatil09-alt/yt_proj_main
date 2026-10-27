import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread
import uuid

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import router
from .analytics import AnalyticsService
from .analytics_api import router as analytics_router
from .config import BASE_DIR, DATABASE_PATH, INITIAL_WORKERS, MAX_WORKERS, METRIC_INTERVAL_SECONDS
from .config import YOUTUBE_API_KEY
from .database import JobHistory
from .services import DashboardService
from .workers import WorkerFactory, WorkerManager
from .workers_model import utc_time
from .youtube import YouTubeDataClient, validate_video_id
from .youtube_api import router as youtube_router
from .youtube_diagnostic import YouTubeDiagnosticManager


def create_app(
    database_path: Path | str = DATABASE_PATH,
    metrics_provider=None,
    worker_factory: WorkerFactory | None = None,
    auto_start: bool = True,
) -> FastAPI:
    history = JobHistory(database_path)
    history.initialize()
    history.seed_default_profiles(INITIAL_WORKERS, utc_time())
    history.restore_profiles_stopped(utc_time())
    session_id = str(uuid.uuid4())
    history.start_session(session_id, utc_time())
    analytics = AnalyticsService(history, session_id)
    manager_kwargs = {
        "history": history,
        "target_workers": INITIAL_WORKERS,
        "max_workers": MAX_WORKERS,
        "worker_factory": worker_factory,
        "session_id": session_id,
    }
    if metrics_provider is not None:
        manager_kwargs["metrics_provider"] = metrics_provider
    workers = WorkerManager(**manager_kwargs)
    youtube = YouTubeDataClient(YOUTUBE_API_KEY)
    youtube_diagnostic = YouTubeDiagnosticManager(history, workers)
    workers.register_external_controller(youtube_diagnostic)
    service_kwargs = {"workers": workers, "history": history, "analytics": analytics}
    if metrics_provider is not None:
        service_kwargs["metrics_provider"] = metrics_provider
    dashboard = DashboardService(**service_kwargs)
    dashboard.youtube_diagnostic = youtube_diagnostic
    dashboard.background_sampling = auto_start
    monitor_stop = Event()
    monitor_thread: Thread | None = None
    retention_thread: Thread | None = None

    def monitor_resources() -> None:
        while not monitor_stop.is_set():
            dashboard.sample_metrics()
            monitor_stop.wait(METRIC_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal monitor_thread, retention_thread
        retention_thread = Thread(
            target=analytics.cleanup_retention, name="analytics-retention", daemon=True
        )
        retention_thread.start()
        if auto_start:
            monitor_thread = Thread(target=monitor_resources, name="resource-monitor", daemon=True)
            monitor_thread.start()
        yield
        if monitor_thread:
            monitor_stop.set()
            await asyncio.to_thread(monitor_thread.join, METRIC_INTERVAL_SECONDS + 1)
        await asyncio.to_thread(workers.shutdown)
        await asyncio.to_thread(analytics.flush)
        await asyncio.to_thread(youtube.close)
        history.end_session(session_id, utc_time())

    app = FastAPI(title="Local Playback Test Dashboard", version="6.0.0", lifespan=lifespan)
    app.state.dashboard = dashboard
    app.state.analytics = analytics
    app.state.session_id = session_id
    app.state.youtube = youtube
    app.state.youtube_diagnostic = youtube_diagnostic
    app.include_router(router)
    app.include_router(analytics_router, prefix="/api")
    app.include_router(analytics_router, prefix="/api/v1", include_in_schema=False)
    app.include_router(youtube_router, prefix="/api")
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    @app.get("/playback-test", response_class=HTMLResponse)
    def playback_test(request: Request):
        return templates.TemplateResponse(request=request, name="playback_test.html")

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request):
        return templates.TemplateResponse(request=request, name="analytics.html")

    @app.get("/youtube-player", response_class=HTMLResponse)
    def youtube_player(request: Request, video_id: str):
        try:
            video_id = validate_video_id(video_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return templates.TemplateResponse(
            request=request, name="youtube_player.html", context={"video_id": video_id},
            headers={"Referrer-Policy": "strict-origin-when-cross-origin"},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
