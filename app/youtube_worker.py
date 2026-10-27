from .browser_worker import BrowserWorker, chrome_driver_factory
from .youtube import validate_video_id
from .youtube_player import open_player, read_player


def visible_driver(profile_path):
    return chrome_driver_factory(profile_path, headless=False)


class YouTubeWorker(BrowserWorker):
    """One selected-video playback using the diagnostic's iframe player."""

    ALLOW_REPEAT = False

    def __init__(self, worker_id, selection, history, **kwargs):
        self.video_id = validate_video_id(selection["video_id"])
        self.video_title = selection.get("video_title", "")
        kwargs.pop("playback_mode", None)
        kwargs.setdefault("driver_factory", visible_driver)
        super().__init__(worker_id, "", history, playback_mode="once", **kwargs)

    def _open_playback(self, driver):
        with self._lock:
            self.playback_state = "loading YouTube player"
        open_player(driver, self.video_id, self._stop_event)

    def _read_playback(self, driver):
        state = read_player(driver)
        return {**state, "currentTime": state.get("time", 0)}

    def snapshot(self):
        return {**super().snapshot(), "video_id": self.video_id,
                "video_title": self.video_title, "playback_mode": "once"}

    def _process_info_unlocked(self):
        return {**super()._process_info_unlocked(), "video_id": self.video_id,
                "video_title": self.video_title}
