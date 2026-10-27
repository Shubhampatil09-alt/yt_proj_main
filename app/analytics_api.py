from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .database import FINAL_STATUSES
from .reporting import ReportExporter


router = APIRouter(prefix="/analytics", tags=["analytics"])


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _filters(
    start: datetime | None,
    end: datetime | None,
    session_id: str | None,
    worker_id: int | None,
    status: str | None,
    bot_id: str | None = None,
) -> dict:
    normalized_start = _iso(start)
    normalized_end = _iso(end)
    if normalized_start and normalized_end and normalized_start > normalized_end:
        raise HTTPException(status_code=422, detail="start must be before end")
    if status and status not in FINAL_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status filter")
    return {
        "start": normalized_start,
        "end": normalized_end,
        "session_id": session_id,
        "worker_id": worker_id,
        "status": status,
        "bot_id": bot_id,
    }


@router.get("/summary")
def summary(
    request: Request, start: datetime | None = None, end: datetime | None = None,
    session_id: str | None = None, worker_id: int | None = Query(None, ge=1),
    status: str | None = None,
    bot_id: str | None = Query(None, max_length=36),
) -> dict:
    request.app.state.analytics.flush()
    return request.app.state.dashboard.history.analytics_summary(
        _filters(start, end, session_id, worker_id, status, bot_id)
    )


@router.get("/sessions")
def sessions(request: Request) -> dict:
    return {"items": request.app.state.dashboard.history.list_sessions()}


@router.get("/youtube-diagnostics")
def youtube_diagnostics(request: Request, limit: int = Query(100, ge=1, le=500)) -> dict:
    return {"items": request.app.state.dashboard.history.list_youtube_diagnostics(limit)}


@router.get("/runs")
def runs(
    request: Request, start: datetime | None = None, end: datetime | None = None,
    session_id: str | None = None, worker_id: int | None = Query(None, ge=1),
    status: str | None = None, page: int = Query(1, ge=1),
    bot_id: str | None = Query(None, max_length=36),
    page_size: int = Query(25, ge=1, le=200),
    sort_by: Literal["started_at", "ended_at", "duration", "status", "worker_id", "progress"] = "started_at",
    sort_order: Literal["asc", "desc"] = "desc",
) -> dict:
    return request.app.state.dashboard.history.list_runs(
        _filters(start, end, session_id, worker_id, status, bot_id),
        page=page, page_size=page_size, sort_by=sort_by, sort_order=sort_order,
    )


@router.get("/runs/{run_id}")
def run_detail(run_id: str, request: Request) -> dict:
    result = request.app.state.dashboard.history.get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return result


@router.get("/resources")
def resources(
    request: Request, start: datetime | None = None, end: datetime | None = None,
    session_id: str | None = None, limit: int = Query(1000, ge=1, le=5000),
) -> dict:
    request.app.state.analytics.flush()
    filters = _filters(start, end, session_id, None, None)
    return {"items": request.app.state.dashboard.history.list_resources(filters, limit)}


@router.get("/errors")
def errors(
    request: Request, start: datetime | None = None, end: datetime | None = None,
    session_id: str | None = None, worker_id: int | None = Query(None, ge=1),
    limit: int = Query(100, ge=1, le=1000),
    bot_id: str | None = Query(None, max_length=36),
) -> dict:
    filters = _filters(start, end, session_id, worker_id, None, bot_id)
    return {"items": request.app.state.dashboard.history.list_errors(filters, limit)}


@router.get("/export")
def export(
    request: Request, format: Literal["csv", "xlsx"] = "csv",
    start: datetime | None = None, end: datetime | None = None,
    session_id: str | None = None, worker_id: int | None = Query(None, ge=1),
    status: str | None = None,
    bot_id: str | None = Query(None, max_length=36),
) -> Response:
    request.app.state.analytics.flush()
    filters = _filters(start, end, session_id, worker_id, status, bot_id)
    exporter = ReportExporter(request.app.state.dashboard.history)
    if format == "csv":
        content = exporter.csv(filters)
        media_type = "text/csv; charset=utf-8"
        filename = "playback-report.csv"
    else:
        content = exporter.xlsx(filters)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "playback-report.xlsx"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
