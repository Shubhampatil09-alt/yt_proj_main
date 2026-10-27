from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from typing import Callable, Protocol

import psutil
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from .database import JobHistory
from .config import HEADLESS_CHROME
from .workers_model import WorkerStatus, utc_time


class DriverLike(Protocol):
    service: object

    def get(self, url: str) -> None: ...
    def execute_script(self, script: str): ...
    def quit(self) -> None: ...


DriverFactory = Callable[[Path], DriverLike]
TerminalCallback = Callable[[str, WorkerStatus], None]


def chrome_driver_factory(profile_path: Path, *, headless: bool = HEADLESS_CHROME) -> DriverLike:
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-extensions")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--window-size=960,640")
    options.add_argument(f"--user-data-dir={profile_path}")
    return webdriver.Chrome(options=options)


class BrowserWorker:
    """One Selenium WebDriver and local HTML5 playback session."""

    POLL_SECONDS = 0.5
    ALLOW_REPEAT = True

    def __init__(
        self,
        worker_id: int,
        playback_url: str,
        history: JobHistory,
        driver_factory: DriverFactory = chrome_driver_factory,
        session_id: str | None = None,
        bot_id: str | None = None,
        bot_name: str | None = None,
        on_terminal: TerminalCallback | None = None,
        playback_mode: str = "once",
    ) -> None:
        self.id = worker_id
        self.playback_url = playback_url
        self.history = history
        self.driver_factory = driver_factory
        self.session_id = session_id or str(uuid.uuid4())
        self.bot_id = bot_id
        self.bot_name = bot_name
        self.on_terminal = on_terminal
        self.playback_mode = playback_mode
        if session_id is None:
            self.history.start_session(self.session_id, utc_time())
        self.run_id: str | None = None
        self.status = WorkerStatus.STOPPED
        self.playback_state = "not started"
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.elapsed_seconds = 0.0
        self.progress_percent = 0.0
        self.current_time = 0.0
        self.duration = 0.0
        self.last_error: str | None = None
        self.stop_reason: str | None = None
        self._driver: DriverLike | None = None
        self._driver_root_pid: int | None = None
        self._managed_processes: dict[int, float] = {}
        self._profile_path: Path | None = None
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._lock = RLock()
        self._started_monotonic: float | None = None
        self.cleanup_verified: bool | None = None
        self.orphaned_pids: list[int] = []

    def start(self) -> bool:
        with self._lock:
            if self.status in WorkerStatus.active():
                return False
            self._stop_event.clear()
            self.run_id = str(uuid.uuid4())
            self.status = WorkerStatus.STARTING
            self.playback_state = "opening browser"
            self.started_at = utc_time()
            self.finished_at = None
            self.last_error = None
            self.stop_reason = None
            self.progress_percent = 0.0
            self.current_time = 0.0
            self.duration = 0.0
            self.cleanup_verified = None
            self.orphaned_pids = []
            self._started_monotonic = time.monotonic()
            self.history.begin_run(
                self.run_id, self.session_id, self.id, self.started_at,
                bot_id=self.bot_id, bot_name=self.bot_name,
            )
            self._thread = Thread(target=self._run, name=f"playback-worker-{self.id}", daemon=True)
            self._thread.start()
            return True

    def stop(self, reason: str = "user_stop") -> bool:
        with self._lock:
            if self.status in {WorkerStatus.STOPPED, WorkerStatus.FAILED, WorkerStatus.STOPPING}:
                return False
            self.status = WorkerStatus.STOPPING
            self.playback_state = "stop requested"
            self.stop_reason = reason
            if self.run_id:
                self.history.record_transition(
                    self.run_id, self.id, WorkerStatus.STOPPING.value, utc_time(), reason
                )
            self._stop_event.set()
            return True

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread and thread is not current_thread():
            thread.join(timeout)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "bot_id": self.bot_id,
                "bot_name": self.bot_name,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "status": self.status.value,
                "playback_state": self.playback_state,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_seconds": round(self._elapsed_unlocked(), 1),
                "progress_percent": round(self.progress_percent, 1),
                "current_time": round(self.current_time, 1),
                "duration": round(self.duration, 1),
                "last_error": self.last_error,
                "stop_reason": self.stop_reason,
                "process": {
                    **self._process_info_unlocked(),
                    "cleanup_verified": self.cleanup_verified,
                    "orphaned_pids": self.orphaned_pids,
                },
            }

    def _run(self) -> None:
        completed = False
        try:
            self._profile_path = Path(tempfile.mkdtemp(prefix=f"playback_worker_{self.id}_"))
            driver = self.driver_factory(self._profile_path)
            with self._lock:
                self._driver = driver
                process = getattr(getattr(driver, "service", None), "process", None)
                self._driver_root_pid = getattr(process, "pid", None)
                self._refresh_processes_unlocked()
            if self._stop_event.is_set():
                return
            self._open_playback(driver)
            if self._stop_event.is_set():
                return
            with self._lock:
                self.status = WorkerStatus.RUNNING
                self.playback_state = "playing"
                if self.run_id:
                    self.history.record_transition(
                        self.run_id, self.id, WorkerStatus.RUNNING.value, utc_time()
                    )

            while not self._stop_event.wait(self.POLL_SECONDS):
                state = self._read_playback(driver)
                with self._lock:
                    self.current_time = float(state.get("currentTime") or 0)
                    self.duration = float(state.get("duration") or 0)
                    self.progress_percent = min(
                        100.0, (self.current_time / self.duration * 100) if self.duration else 0.0
                    )
                    if state.get("error"):
                        raise RuntimeError(str(state["error"]))
                    if state.get("ended"):
                        if self.ALLOW_REPEAT and self.playback_mode == "repeat":
                            driver.execute_script(
                                "const v=document.getElementById('testVideo'); v.currentTime=0; return v.play();"
                            )
                            self.current_time = 0.0
                            self.progress_percent = 0.0
                            self.playback_state = "playing"
                            continue
                        completed = True
                        self.progress_percent = 100.0
                        self.playback_state = "completed"
                        break
                    self.playback_state = state.get("state") or ("paused" if state.get("paused") else "playing")

            with self._lock:
                if not completed:
                    self.playback_state = "stopped"
                self.status = WorkerStatus.STOPPING
        except Exception as error:
            with self._lock:
                self.status = WorkerStatus.FAILED
                self.playback_state = "error"
                self.last_error = self._friendly_error(error)
        finally:
            with self._lock:
                final_process_info = self._process_info_unlocked()
            self._close_driver()
            with self._lock:
                self.elapsed_seconds = self._elapsed_unlocked()
                self.finished_at = utc_time()
                if self.status != WorkerStatus.FAILED:
                    self.status = WorkerStatus.STOPPED
                if completed:
                    final_status = "completed"
                elif self.last_error:
                    final_status = "failed"
                elif (self.stop_reason or "").startswith("safety:"):
                    final_status = "safety_stopped"
                else:
                    final_status = "stopped"
                if self.run_id:
                    self.history.record_transition(
                        self.run_id, self.id, final_status, self.finished_at, self.stop_reason
                    )
                    self.history.finalize_run(
                        self.run_id,
                        ended_at=self.finished_at,
                        execution_duration=self.elapsed_seconds,
                        final_status=final_status,
                        playback_duration=self.duration,
                        playback_progress=self.progress_percent,
                        error_message=self.last_error,
                        browser_process_info=final_process_info,
                        stop_reason=self.stop_reason,
                    )
                terminal_status = self.status
            if self.on_terminal and self.bot_id:
                try:
                    self.on_terminal(self.bot_id, terminal_status)
                except Exception:
                    pass

    def _open_playback(self, driver) -> None:
        driver.get(self.playback_url)
        WebDriverWait(driver, 20).until(
            lambda item: bool(item.execute_script(
                "const v=document.getElementById('testVideo'); return v && v.readyState >= 2;"
            )) or self._stop_event.is_set()
        )
        if not self._stop_event.is_set():
            driver.execute_script(
                "const v=document.getElementById('testVideo'); v.muted=true; return v.play();"
            )

    def _read_playback(self, driver) -> dict:
        return driver.execute_script("""
            const v = document.getElementById('testVideo');
            return {currentTime:v.currentTime||0, duration:v.duration||0,
                    paused:v.paused, ended:v.ended,
                    error:v.error ? (v.error.message || ('Media error '+v.error.code)) : null};
        """)

    def _close_driver(self) -> None:
        driver = self._driver
        with self._lock:
            self._refresh_processes_unlocked()
            managed = dict(self._managed_processes)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        orphaned = self._terminate_remaining_managed_processes(managed)
        with self._lock:
            self._driver = None
            self.orphaned_pids = orphaned
            self.cleanup_verified = not orphaned
        if self._profile_path:
            shutil.rmtree(self._profile_path, ignore_errors=True)

    def _refresh_processes_unlocked(self) -> None:
        if self._driver_root_pid is None:
            return
        try:
            root = psutil.Process(self._driver_root_pid)
            children = root.children(recursive=True)
            processes = [root, *children]
            self._managed_processes.update({process.pid: process.create_time() for process in processes})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def _process_info_unlocked(self) -> dict:
        self._refresh_processes_unlocked()
        driver_mb = chrome_mb = 0.0
        live_pids, chrome_pids, processes = [], [], []
        for pid, created_at in sorted(self._managed_processes.items()):
            try:
                process = psutil.Process(pid)
                if process.create_time() != created_at:
                    continue
                live_pids.append(pid)
                memory_mb = process.memory_info().rss / 1024**2
                kind = "chromedriver" if pid == self._driver_root_pid else "chrome"
                if kind == "chromedriver":
                    driver_mb += memory_mb
                else:
                    chrome_mb += memory_mb
                    chrome_pids.append(pid)
                processes.append({"pid": pid, "created_at": created_at, "kind": kind})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {
            "driver_pid": self._driver_root_pid,
            "pids": live_pids,
            "chrome_pids": chrome_pids,
            "processes": processes,
            "chromedriver_memory_mb": round(driver_mb, 1),
            "chrome_memory_mb": round(chrome_mb, 1),
            "memory_mb": round(driver_mb + chrome_mb, 1),
        }

    @staticmethod
    def _terminate_remaining_managed_processes(managed: dict[int, float]) -> list[int]:
        remaining = []
        for pid, created_at in managed.items():
            try:
                process = psutil.Process(pid)
                if process.create_time() == created_at:
                    remaining.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
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
        return [process.pid for process in alive]

    def _elapsed_unlocked(self) -> float:
        if self._started_monotonic is None:
            return self.elapsed_seconds
        if self.status in {WorkerStatus.STOPPED, WorkerStatus.FAILED} and self.elapsed_seconds:
            return self.elapsed_seconds
        return time.monotonic() - self._started_monotonic

    @staticmethod
    def _friendly_error(error: Exception) -> str:
        message = str(error).splitlines()[0].strip()
        if isinstance(error, WebDriverException) and not message:
            return "Chrome WebDriver terminated unexpectedly."
        return message[:500] or error.__class__.__name__
