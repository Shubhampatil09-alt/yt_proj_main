from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field


router = APIRouter(prefix="/api")


class BotCreate(BaseModel):
    display_name: str | None = Field(None, max_length=80)
    settings: dict[str, Any] = Field(default_factory=dict)


class BotUpdate(BaseModel):
    display_name: str | None = Field(None, max_length=80)
    settings: dict[str, Any] | None = None


def _control(operation):
    try:
        return operation()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Bot was not found.") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/metrics")
def metrics(request: Request) -> dict:
    return request.app.state.dashboard.metrics()


@router.get("/dashboard/snapshot")
def dashboard_snapshot(request: Request) -> dict:
    """One polling request replaces separate metrics, bots, logs, and selection calls."""
    dashboard = request.app.state.dashboard
    metrics = dashboard.metrics()
    bots = dashboard.workers.state()
    selection = dashboard.history.get_youtube_selection()
    diagnostic = request.app.state.youtube_diagnostic.snapshot()
    eligibility = dashboard.workers.browser_start_eligibility()
    if not request.app.state.youtube.configured:
        eligibility = {
            "allowed": False,
            "reason": "YouTube API configuration is unavailable; set YOUTUBE_API_KEY and restart.",
        }
    elif selection is None:
        eligibility = {"allowed": False, "reason": "Select and save a YouTube video first."}
    elif diagnostic["status"] in {"Starting", "Running", "Stopping"}:
        eligibility = {
            "allowed": False,
            "reason": f"A diagnostic is already {diagnostic['status'].lower()}.",
        }
    return {
        "metrics": metrics,
        "bots": bots,
        "logs": dashboard.workers.logs(),
        "youtube": {
            "configured": request.app.state.youtube.configured,
            "selection": selection,
            "diagnostic": diagnostic,
            "eligibility": eligibility,
            "latest_result": dashboard.history.latest_youtube_diagnostic(
                selection["video_id"] if selection else None
            ),
        },
    }


@router.get("/performance")
def performance(request: Request) -> dict:
    return request.app.state.dashboard.performance()


@router.get("/workers")
def workers(request: Request) -> dict:
    return request.app.state.dashboard.workers.state()


@router.get("/bots")
def bots(request: Request) -> dict:
    return request.app.state.dashboard.workers.state()


@router.post("/bots", status_code=201)
def create_bot(payload: BotCreate, request: Request) -> dict:
    return _control(lambda: request.app.state.dashboard.workers.create_bot(
        payload.display_name, payload.settings
    ))


@router.post("/bots/start-all")
def start_all_bots(request: Request) -> dict:
    return _control(request.app.state.dashboard.workers.start_all)


@router.post("/bots/stop-all")
def stop_all_bots(request: Request) -> dict:
    return request.app.state.dashboard.workers.stop_all()


@router.patch("/bots/{bot_id}")
def update_bot(bot_id: str, payload: BotUpdate, request: Request) -> dict:
    return _control(lambda: request.app.state.dashboard.workers.update_bot(
        bot_id, payload.display_name, payload.settings
    ))


@router.post("/bots/{bot_id}/start")
def start_bot(bot_id: str, request: Request) -> dict:
    return _control(lambda: request.app.state.dashboard.workers.start_bot(bot_id))


@router.post("/bots/{bot_id}/stop")
def stop_bot(bot_id: str, request: Request) -> dict:
    return _control(lambda: request.app.state.dashboard.workers.stop_bot(bot_id))


@router.post("/bots/{bot_id}/restart")
def restart_bot(bot_id: str, request: Request) -> dict:
    return _control(lambda: request.app.state.dashboard.workers.restart_bot(bot_id))


@router.delete("/bots/{bot_id}")
def remove_bot(bot_id: str, request: Request, confirm: bool = Query(False)) -> dict:
    return _control(lambda: request.app.state.dashboard.workers.remove_bot(bot_id, confirm))


@router.post("/workers/add")
def add_worker(request: Request) -> dict:
    try:
        return request.app.state.dashboard.workers.add()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/workers/remove")
def remove_worker(request: Request) -> dict:
    try:
        return request.app.state.dashboard.workers.remove()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/workers/start-all")
def start_all(request: Request) -> dict:
    try:
        return request.app.state.dashboard.workers.start_all()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/workers/stop-all")
def stop_all(request: Request) -> dict:
    return request.app.state.dashboard.workers.stop_all()


@router.post("/workers/{worker_id}/stop")
def stop_worker(worker_id: int, request: Request) -> dict:
    try:
        return request.app.state.dashboard.workers.stop(worker_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"Worker {worker_id} was not found.") from error


@router.post("/workers/increase-limit")
def increase_limit(request: Request) -> dict:
    try:
        return request.app.state.dashboard.workers.increase_limit()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/logs")
def logs(request: Request) -> dict:
    return {"logs": request.app.state.dashboard.workers.logs()}


@router.get("/jobs")
def jobs(request: Request) -> dict:
    return {"jobs": request.app.state.dashboard.history.recent()}
