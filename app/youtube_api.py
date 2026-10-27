from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .workers_model import utc_time
from .youtube import YouTubeAPIError, validate_channel_id, validate_video_id


router = APIRouter(prefix="/youtube", tags=["youtube"])


class VideoSelection(BaseModel):
    channel_id: str
    video_id: str


def _youtube_call(operation):
    try:
        return operation()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except YouTubeAPIError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"message": str(error), "code": error.code},
        ) from error


@router.get("/status")
def status(request: Request) -> dict:
    return {
        "configured": request.app.state.youtube.configured,
        "credential_source": "environment" if request.app.state.youtube.configured else None,
    }


@router.get("/channels")
def search_channels(request: Request, q: str = Query(..., min_length=1, max_length=200)) -> dict:
    return {"items": _youtube_call(lambda: request.app.state.youtube.search_channels(q))}


@router.get("/channels/{channel_id}/videos")
def channel_videos(
    channel_id: str, request: Request,
    page_token: str | None = Query(None, max_length=200),
) -> dict:
    def videos():
        youtube = request.app.state.youtube
        validated = validate_channel_id(channel_id)
        if hasattr(youtube, "channel_videos_page"):
            return youtube.channel_videos_page(validated, page_token)
        return {"items": youtube.channel_videos(validated), "next_page_token": None}
    return _youtube_call(videos)


@router.get("/selection")
def get_selection(request: Request) -> dict:
    return {"selection": request.app.state.dashboard.history.get_youtube_selection()}


@router.post("/selection")
def save_selection(payload: VideoSelection, request: Request) -> dict:
    def select():
        channel_id = validate_channel_id(payload.channel_id)
        video_id = validate_video_id(payload.video_id)
        channels = request.app.state.youtube.search_channels(channel_id)
        if not channels:
            raise YouTubeAPIError("The selected channel is unavailable.", "channel_unavailable", 404)
        video = request.app.state.youtube.verify_video(channel_id, video_id)
        return request.app.state.dashboard.history.save_youtube_selection(
            channels[0], video, utc_time()
        )
    return {"selection": _youtube_call(select)}


@router.get("/diagnostic")
def diagnostic(request: Request) -> dict:
    return request.app.state.youtube_diagnostic.snapshot()


@router.get("/diagnostic/latest")
def latest_diagnostic(request: Request) -> dict:
    selection = request.app.state.dashboard.history.get_youtube_selection()
    return {"result": request.app.state.dashboard.history.latest_youtube_diagnostic(
        selection["video_id"] if selection else None
    )}


@router.post("/diagnostic/start")
def start_diagnostic(request: Request) -> dict:
    def start():
        request.app.state.dashboard.metrics()
        selected = request.app.state.dashboard.history.get_youtube_selection()
        if selected is None:
            raise YouTubeAPIError("Select a YouTube video first.", "selection_required", 409)
        video = request.app.state.youtube.verify_video(
            selected["channel_id"], selected["video_id"]
        )
        if not video.get("embeddable"):
            raise YouTubeAPIError(
                "This video does not permit embedded playback diagnostics.",
                "not_embeddable", 409,
            )
        return request.app.state.youtube_diagnostic.start(video)
    return _youtube_call(start)


@router.post("/diagnostic/stop")
def stop_diagnostic(request: Request) -> dict:
    request.app.state.youtube_diagnostic.stop("user_stop")
    return request.app.state.youtube_diagnostic.snapshot()
