from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import YOUTUBE_API_TIMEOUT_SECONDS, YOUTUBE_MAX_RESULTS


API_ROOT = "https://www.googleapis.com/youtube/v3"
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


class YouTubeAPIError(RuntimeError):
    def __init__(self, message: str, code: str = "api_error", status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def validate_channel_id(value: str) -> str:
    value = value.strip()
    if not CHANNEL_ID_RE.fullmatch(value):
        raise ValueError("Invalid YouTube channel ID.")
    return value


def validate_video_id(value: str) -> str:
    value = value.strip()
    if not VIDEO_ID_RE.fullmatch(value):
        raise ValueError("Invalid YouTube video ID.")
    return value


def duration_seconds(value: str) -> int:
    match = ISO_DURATION_RE.fullmatch(value or "")
    if not match:
        return 0
    parts = {key: int(number or 0) for key, number in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def _thumbnail(snippet: dict[str, Any]) -> str | None:
    choices = snippet.get("thumbnails") or {}
    for key in ("medium", "high", "default"):
        if choices.get(key, {}).get("url"):
            return str(choices[key]["url"])
    return None


def parse_channel_query(query: str) -> tuple[str, str]:
    value = query.strip()
    if not value or len(value) > 200:
        raise ValueError("Enter a channel name, @handle, or YouTube channel URL.")
    if CHANNEL_ID_RE.fullmatch(value):
        return "id", value
    if HANDLE_RE.fullmatch(value):
        return "handle", value[1:]
    if value.lower().startswith(("http://", "https://")):
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            raise ValueError("Only youtube.com channel URLs are supported.")
        segments = [part for part in parsed.path.split("/") if part]
        if len(segments) >= 2 and segments[0] == "channel" and CHANNEL_ID_RE.fullmatch(segments[1]):
            return "id", segments[1]
        if segments and HANDLE_RE.fullmatch(segments[0]):
            return "handle", segments[0][1:]
        if segments:
            return "name", segments[-1]
        raise ValueError("The YouTube channel URL is incomplete.")
    return "name", value


class YouTubeDataClient:
    """Small server-side YouTube Data API v3 client; the key never leaves Python."""

    def __init__(self, api_key: str, client: Any | None = None) -> None:
        self._api_key = api_key.strip()
        self._client = client or httpx.Client(timeout=YOUTUBE_API_TIMEOUT_SECONDS)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close:
            close()

    def search_channels(self, query: str) -> list[dict[str, Any]]:
        kind, value = parse_channel_query(query)
        if kind == "id":
            payload = self._get("channels", part="snippet,statistics,contentDetails", id=value)
            return [self._channel(item) for item in payload.get("items", [])]
        if kind == "handle":
            payload = self._get(
                "channels", part="snippet,statistics,contentDetails", forHandle=value
            )
            return [self._channel(item) for item in payload.get("items", [])]
        search = self._get(
            "search", part="snippet", q=value, type="channel", maxResults=10
        )
        ids = [item.get("id", {}).get("channelId") for item in search.get("items", [])]
        ids = [item for item in ids if item and CHANNEL_ID_RE.fullmatch(item)]
        if not ids:
            return []
        payload = self._get(
            "channels", part="snippet,statistics,contentDetails", id=",".join(ids)
        )
        channels = {item["id"]: self._channel(item) for item in payload.get("items", [])}
        return [channels[channel_id] for channel_id in ids if channel_id in channels]

    def channel_videos(self, channel_id: str) -> list[dict[str, Any]]:
        return self.channel_videos_page(channel_id)["items"]

    def channel_videos_page(
        self, channel_id: str, page_token: str | None = None
    ) -> dict[str, Any]:
        channel_id = validate_channel_id(channel_id)
        channel_payload = self._get("channels", part="contentDetails", id=channel_id)
        items = channel_payload.get("items", [])
        if not items:
            raise YouTubeAPIError("The selected channel is unavailable.", "channel_unavailable", 404)
        playlist_id = items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not playlist_id:
            return {"items": [], "next_page_token": None}
        parameters: dict[str, Any] = {
            "part": "contentDetails", "playlistId": playlist_id,
            "maxResults": YOUTUBE_MAX_RESULTS,
        }
        if page_token:
            if len(page_token) > 200:
                raise ValueError("Invalid YouTube pagination token.")
            parameters["pageToken"] = page_token
        uploads = self._get("playlistItems", **parameters)
        ids = [item.get("contentDetails", {}).get("videoId") for item in uploads.get("items", [])]
        ids = [item for item in ids if item and VIDEO_ID_RE.fullmatch(item)]
        if not ids:
            return {"items": [], "next_page_token": uploads.get("nextPageToken")}
        payload = self._get(
            "videos", part="snippet,contentDetails,statistics,status", id=",".join(ids)
        )
        videos = {item["id"]: self._video(item) for item in payload.get("items", [])}
        # Checking snippet.channelId here excludes moved/deleted/private playlist entries.
        return {
            "items": [videos[video_id] for video_id in ids
                      if video_id in videos and videos[video_id]["channel_id"] == channel_id],
            "next_page_token": uploads.get("nextPageToken"),
        }

    def verify_video(self, channel_id: str, video_id: str) -> dict[str, Any]:
        channel_id = validate_channel_id(channel_id)
        video_id = validate_video_id(video_id)
        payload = self._get(
            "videos", part="snippet,contentDetails,statistics,status", id=video_id
        )
        items = payload.get("items", [])
        if not items:
            raise YouTubeAPIError("The selected video is unavailable.", "video_unavailable", 404)
        video = self._video(items[0])
        if video["channel_id"] != channel_id:
            raise YouTubeAPIError(
                "The selected video does not belong to the selected channel.",
                "channel_mismatch", 409,
            )
        if video["privacy_status"] != "public":
            raise YouTubeAPIError("The selected video is not public.", "video_unavailable", 409)
        return video

    def _get(self, resource: str, **params: Any) -> dict[str, Any]:
        if not self.configured:
            raise YouTubeAPIError(
                "YouTube integration is not configured. Set YOUTUBE_API_KEY and restart.",
                "not_configured", 503,
            )
        try:
            response = self._client.get(
                f"{API_ROOT}/{resource}", params={**params, "key": self._api_key}
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as error:
            raise YouTubeAPIError("The YouTube API is temporarily unreachable.") from error
        if response.status_code >= 400:
            self._raise_api_error(response)
        try:
            return response.json()
        except ValueError as error:
            raise YouTubeAPIError("The YouTube API returned an invalid response.") from error

    @staticmethod
    def _raise_api_error(response: Any) -> None:
        reasons: set[str] = set()
        try:
            details = response.json().get("error", {}).get("errors", [])
            reasons = {str(item.get("reason", "")) for item in details}
        except (ValueError, AttributeError):
            pass
        if response.status_code == 429 or reasons & {
            "quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"
        }:
            raise YouTubeAPIError(
                "YouTube API quota is exhausted. Try again after the quota resets.",
                "quota_exceeded", 429,
            )
        if response.status_code in {401, 403}:
            raise YouTubeAPIError(
                "YouTube API credentials were rejected or lack permission.",
                "credentials_rejected", 502,
            )
        raise YouTubeAPIError("YouTube API request failed.", status_code=502)

    @staticmethod
    def _channel(item: dict[str, Any]) -> dict[str, Any]:
        snippet, statistics = item.get("snippet", {}), item.get("statistics", {})
        related = item.get("contentDetails", {}).get("relatedPlaylists", {})
        return {
            "channel_id": item.get("id"),
            "title": unescape(str(snippet.get("title", ""))),
            "description": unescape(str(snippet.get("description", ""))),
            "thumbnail_url": _thumbnail(snippet),
            "published_at": snippet.get("publishedAt"),
            "subscriber_count": int(statistics["subscriberCount"]) if statistics.get("subscriberCount") else None,
            "video_count": int(statistics["videoCount"]) if statistics.get("videoCount") else None,
            "uploads_playlist_id": related.get("uploads"),
        }

    @staticmethod
    def _video(item: dict[str, Any]) -> dict[str, Any]:
        snippet = item.get("snippet", {})
        details, statistics, status = (
            item.get("contentDetails", {}), item.get("statistics", {}), item.get("status", {})
        )
        video_id = str(item.get("id", ""))
        return {
            "video_id": video_id,
            "channel_id": snippet.get("channelId"),
            "channel_title": unescape(str(snippet.get("channelTitle", ""))),
            "title": unescape(str(snippet.get("title", ""))),
            "thumbnail_url": _thumbnail(snippet),
            "published_at": snippet.get("publishedAt"),
            "duration": details.get("duration", "PT0S"),
            "duration_seconds": duration_seconds(str(details.get("duration", ""))),
            "privacy_status": status.get("privacyStatus"),
            "embeddable": bool(status.get("embeddable", False)),
            "statistics": {
                key: int(value) for key, value in statistics.items()
                if key in {"viewCount", "likeCount", "commentCount"} and str(value).isdigit()
            },
            "url": f"https://www.youtube.com/watch?v={video_id}",
        }
