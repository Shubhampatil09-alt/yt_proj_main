from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from typing import Callable

import psutil

from .browser_worker import DriverLike, chrome_driver_factory
from .database import JobHistory
from .workers import WorkerManager
from .workers_model import utc_time
from .youtube import validate_video_id
from .youtube_player import open_player, read_player


def diagnostic_driver_factory(profile_path: Path) -> DriverLike:
    return chrome_driver_factory(profile_path, headless=False)


class YouTubeDiagnosticManager:
    """One manually initiated diagnostic, deliberately isolated from bot workers."""

    SLOT_ID = "youtube-manual-diagnostic"

    def __init__(
        self, history: JobHistory, workers: WorkerManager,
        driver_factory: Callable[[Path], DriverLike] = diagnostic_driver_factory,
    ) -> None:
        self.history = history
        self.workers = workers
        self.driver_factory = driver_factory
        self.status = "Idle"
        self.diagnostic_id: str | None = None
        self.video_id: str | None = None
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.playback_seconds = 0.0
        self.elapsed_seconds = 0.0
        self.playback_status = "not started"
        self.browser_status = "closed"
        self.last_error: str | None = None
        self.stop_reason: str | None = None
        self._driver: DriverLike | None = None
        self._driver_pid: int | None = None
        self._managed: dict[int, float] = {}
        self._profile_path: Path | None = None
        self._thread: Thread | None = None
        self._stop = Event()
        self._lock = RLock()
        self._started_monotonic: float | None = None
        self.cleanup_verified: bool | None = None
        self.orphaned_pids: list[int] = []

    def start(self, selection: dict) -> dict:
        with self._lock:
            if self.status in {"Starting", "Running", "Stopping"}:
                raise ValueError("A YouTube diagnostic is already active.")
            video_id = validate_video_id(str(selection["video_id"]))
            if selection.get("embeddable") is False:
                raise ValueError("This video does not allow embedded playback diagnostics.")
            self.workers.reserve_external_slot(self.SLOT_ID)
            try:
                self.status = "Starting"
                self.diagnostic_id = str(uuid.uuid4())
                self.video_id = video_id
                self.started_at = utc_time()
                self.ended_at = None
                self.playback_seconds = 0.0
                self.elapsed_seconds = 0.0
                self.playback_status = "waiting for browser"
                self.browser_status = "opening"
                self.last_error = None
                self.stop_reason = None
                self.cleanup_verified = None
                self.orphaned_pids = []
                self._driver_pid = None
                self._managed = {}
                self._stop.clear()
                self._started_monotonic = time.monotonic()
                self.history.start_youtube_diagnostic(
                    self.diagnostic_id, video_id, self.started_at
                )
                self._thread = Thread(
                    target=self._run, name="youtube-manual-diagnostic", daemon=True
                )
                self._thread.start()
                return self.snapshot()
            except Exception:
                self.status = "Failed"
                self.workers.release_external_slot(self.SLOT_ID)
                raise

    def stop(self, reason: str = "user_stop") -> bool:
        with self._lock:
            if self.status not in {"Starting", "Running"}:
                return False
            self.status = "Stopping"
            self.playback_status = "stop requested"
            self.stop_reason = reason
            self._stop.set()
            return True

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread and thread is not current_thread():
            thread.join(timeout)

    def snapshot(self) -> dict:
        with self._lock:
            process = self._process_info_unlocked()
            return {
                "diagnostic_id": self.diagnostic_id,
                "video_id": self.video_id,
                "status": self.status,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "playback_seconds": round(self.playback_seconds, 1),
                "elapsed_seconds": round(self._elapsed_unlocked(), 1),
                "maximum_seconds": None,
                "playback_status": self.playback_status,
                "browser_status": self.browser_status,
                "last_error": self.last_error,
                "stop_reason": self.stop_reason,
                "process": process,
                "cleanup_verified": self.cleanup_verified,
                "orphaned_pids": self.orphaned_pids,
            }

    def _run(self) -> None:
        final_status = "Stopped"
        try:
            self._profile_path = Path(tempfile.mkdtemp(prefix="youtube_diagnostic_"))
            driver = self.driver_factory(self._profile_path)
            with self._lock:
                self._driver = driver
                process = getattr(getattr(driver, "service", None), "process", None)
                self._driver_pid = getattr(process, "pid", None)
                self.browser_status = "open"
                self.playback_status = "loading player"
                self._refresh_processes_unlocked()
            if self._stop.is_set():
                return
            open_player(driver, self.video_id, self._stop)
            if self._stop.is_set():
                return
            with self._lock:
                if self._stop.is_set():
                    return
                self.status = "Running"
                self.playback_status = "player ready"
            while not self._stop.wait(0.5):
                state = read_player(driver)
                with self._lock:
                    self.playback_seconds = float(state.get("time") or 0)
                    self.playback_status = state.get("state") or ("paused" if state.get("paused") else "playing")
                if state.get("error"):
                    raise RuntimeError(str(state["error"]))
                if state.get("ended"):
                    final_status = "Completed"
                    with self._lock:
                        self.playback_status = "completed"
                    break
        except Exception as error:
            final_status = "Failed"
            with self._lock:
                self.last_error = (str(error).splitlines()[0] or error.__class__.__name__)[:500]
                self.playback_status = "error"
        finally:
            with self._lock:
                process_info = self._process_info_unlocked()
            self._close_driver()
            with self._lock:
                if final_status != "Failed" and self.stop_reason:
                    final_status = "Safety Stopped" if self.stop_reason.startswith("safety:") else "Stopped"
                self.status = final_status
                self.elapsed_seconds = self._elapsed_unlocked()
                self.ended_at = utc_time()
                diagnostic_id = self.diagnostic_id
            if diagnostic_id:
                self.history.finish_youtube_diagnostic(
                    diagnostic_id, ended_at=self.ended_at, status=final_status,
                    elapsed_seconds=self.elapsed_seconds, playback_seconds=self.playback_seconds,
                    playback_status=self.playback_status, error_message=self.last_error,
                    process_info=process_info, stop_reason=self.stop_reason,
                    cleanup_verified=self.cleanup_verified, orphaned_pids=self.orphaned_pids,
                )
            self.workers.release_external_slot(self.SLOT_ID)

    def _close_driver(self) -> None:
        driver = self._driver
        with self._lock:
            self._refresh_processes_unlocked()
            managed = dict(self._managed)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        remaining = []
        for pid, created_at in managed.items():
            try:
                process = psutil.Process(pid)
                if process.create_time() == created_at:
                    remaining.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(remaining, timeout=2)
        for process in alive:
            try:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(alive, timeout=2)
        for process in alive:
            try:
                process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(alive, timeout=1)
        with self._lock:
            self._driver = None
            self.browser_status = "closed"
            self.orphaned_pids = [process.pid for process in alive]
            self.cleanup_verified = not self.orphaned_pids
        if self._profile_path:
            shutil.rmtree(self._profile_path, ignore_errors=True)

    def _refresh_processes_unlocked(self) -> None:
        if self._driver_pid is None:
            return
        try:
            root = psutil.Process(self._driver_pid)
            processes = [root, *root.children(recursive=True)]
            self._managed.update({item.pid: item.create_time() for item in processes})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def _process_info_unlocked(self) -> dict:
        self._refresh_processes_unlocked()
        driver_mb = chrome_mb = 0.0
        pids = []
        for pid, created_at in sorted(self._managed.items()):
            try:
                process = psutil.Process(pid)
                if process.create_time() != created_at:
                    continue
                memory = process.memory_info().rss / 1024**2
                pids.append(pid)
                if pid == self._driver_pid:
                    driver_mb += memory
                else:
                    chrome_mb += memory
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return {
            "driver_pid": self._driver_pid, "pids": pids,
            "chromedriver_memory_mb": round(driver_mb, 1),
            "chrome_memory_mb": round(chrome_mb, 1),
            "memory_mb": round(driver_mb + chrome_mb, 1),
        }

    def _elapsed_unlocked(self) -> float:
        if self._started_monotonic is None:
            return self.elapsed_seconds
        if self.ended_at:
            return self.elapsed_seconds
        return time.monotonic() - self._started_monotonic
