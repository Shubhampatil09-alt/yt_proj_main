from __future__ import annotations

import inspect
import math
import time
import uuid
from threading import RLock
from typing import Callable, Protocol

import psutil

from .youtube_worker import YouTubeWorker
from .config import BOT_RAM_ESTIMATE_MB, MAX_WORKERS, MIN_AVAILABLE_RAM_GB, RAM_START_BLOCK_PERCENT
from .database import JobHistory
from .workers_model import WorkerStatus, utc_time


class ManagedWorker(Protocol):
    id: int
    status: WorkerStatus
    def start(self) -> bool: ...
    def stop(self, reason: str = "user_stop") -> bool: ...
    def join(self, timeout: float | None = None) -> None: ...
    def snapshot(self) -> dict: ...


WorkerFactory = Callable[..., ManagedWorker]


class WorkerManager:
    """Thread-safe persistent bot profiles with disposable browser executions."""

    def __init__(self, history: JobHistory, target_workers: int = 1,
                 max_workers: int = MAX_WORKERS, worker_factory: WorkerFactory | None = None,
                 metrics_provider=psutil, session_id: str | None = None) -> None:
        self.target_workers = min(target_workers, max_workers)
        self.max_workers = max_workers
        self.history = history
        self.metrics_provider = metrics_provider
        self.session_id = session_id
        self._worker_factory = worker_factory
        self._workers: dict[int, ManagedWorker] = {}
        self._current: dict[str, ManagedWorker] = {}
        self._external_slots: set[str] = set()
        self._external_controllers: list[object] = []
        self._next_id = history.next_worker_id()
        self._logs: list[dict[str, str]] = []
        self._safety_reason: str | None = None
        self._safety_latched = False
        self._ram_estimate_mb = BOT_RAM_ESTIMATE_MB
        self._external_memory_mb = 0.0
        self._lock = RLock()
        self._log_unlocked("info", f"Manager ready; maximum concurrency is {self.max_workers}.")

    def create_bot(self, display_name: str | None = None, settings: dict | None = None) -> dict:
        self._validate_settings(settings)
        with self._lock:
            profiles = self.history.list_bot_profiles()
            name = (display_name or f"Bot {len(profiles) + 1:03d}").strip()
            if not name or len(name) > 80:
                raise ValueError("Bot name must contain 1 to 80 characters.")
            profile = self.history.create_bot_profile(str(uuid.uuid4()), name, utc_time(), settings or {})
            self._log_unlocked("success", f"Created saved bot {name}.")
            return self._bot_snapshot_unlocked(profile)

    def update_bot(self, bot_id: str, display_name: str | None = None,
                   settings: dict | None = None) -> dict:
        self._validate_settings(settings)
        with self._lock:
            profile = self._profile_unlocked(bot_id)
            if settings is not None:
                settings = {**profile["settings"], **settings}
            if display_name is not None:
                display_name = display_name.strip()
                if not display_name or len(display_name) > 80:
                    raise ValueError("Bot name must contain 1 to 80 characters.")
            self.history.update_bot_profile(bot_id, display_name=display_name, settings=settings,
                                            timestamp=utc_time())
            profile = self._profile_unlocked(bot_id)
            worker = self._current.get(bot_id)
            if worker is not None and not isinstance(worker, YouTubeWorker):
                worker.playback_mode = profile["settings"].get("playback_mode", "repeat")
            self._log_unlocked("info", f"Updated saved bot {profile['display_name']}.")
            return self._bot_snapshot_unlocked(profile)

    def start_bot(self, bot_id: str) -> dict:
        with self._lock:
            self._start_bot_unlocked(bot_id)
            return self._snapshot_unlocked()

    def stop_bot(self, bot_id: str, reason: str = "user_stop") -> dict:
        with self._lock:
            profile = self._profile_unlocked(bot_id)
            worker = self._current.get(bot_id)
            if worker is None or worker.status in {WorkerStatus.STOPPED, WorkerStatus.FAILED}:
                self.history.update_bot_profile(bot_id, status=WorkerStatus.STOPPED.value,
                                                timestamp=utc_time())
                return self._snapshot_unlocked()
            if worker.stop(reason):
                self.history.update_bot_profile(bot_id, status=WorkerStatus.STOPPING.value,
                                                timestamp=utc_time())
                self._log_unlocked("info", f"{profile['display_name']} stop requested.")
            return self._snapshot_unlocked()

    def restart_bot(self, bot_id: str, timeout: float = 15.0) -> dict:
        with self._lock:
            profile = self._profile_unlocked(bot_id)
            worker = self._current.get(bot_id)
            if worker and worker.status in WorkerStatus.active():
                worker.stop("user_restart")
                self.history.update_bot_profile(bot_id, status=WorkerStatus.STOPPING.value,
                                                timestamp=utc_time())
                worker.join(timeout)
                if worker.status in WorkerStatus.active():
                    raise ValueError("Restart timed out while waiting for Chrome cleanup.")
            self.history.update_bot_profile(bot_id, status=WorkerStatus.STOPPED.value,
                                            timestamp=utc_time())
            self._assert_start_safe_unlocked()
            self._start_bot_unlocked(bot_id)
            self._log_unlocked("success", f"{profile['display_name']} restarted with a fresh run.")
            return self._snapshot_unlocked()

    def remove_bot(self, bot_id: str, confirmed: bool = False) -> dict:
        with self._lock:
            profile = self._profile_unlocked(bot_id)
            if not confirmed:
                raise ValueError("Removal requires explicit confirmation.")
            if self._effective_status_unlocked(bot_id, profile) != WorkerStatus.STOPPED:
                raise ValueError("A bot can be removed only after it is fully stopped.")
            self._current.pop(bot_id, None)
            self.history.delete_bot_profile(bot_id)
            self._log_unlocked("warning", f"Removed {profile['display_name']}; history was retained.")
            return self._snapshot_unlocked()

    # Phase 1/2 compatibility adapters.
    def add(self) -> dict:
        with self._lock:
            self._assert_start_safe_unlocked()
            if self._active_browser_count_unlocked() >= self.max_workers:
                raise ValueError(f"Maximum concurrency is {self.max_workers}. Use Increase limit explicitly first.")
            profile = next((p for p in self.history.list_bot_profiles()
                            if self._effective_status_unlocked(p["bot_id"], p)
                            in {WorkerStatus.STOPPED, WorkerStatus.FAILED}), None)
            if profile is None:
                profile = self.history.create_bot_profile(
                    str(uuid.uuid4()), f"Bot {len(self.history.list_bot_profiles()) + 1:03d}",
                    utc_time(), {})
            self._start_bot_unlocked(profile["bot_id"])
            return self._snapshot_unlocked()

    def remove(self) -> dict:
        with self._lock:
            active = [w for w in self._current.values() if w.status in WorkerStatus.active()]
            if not active:
                raise ValueError("No active workers are available to stop.")
            worker = max(active, key=lambda item: item.id)
            return self.stop_bot(self._bot_id_for_worker_unlocked(worker.id), "user_remove")

    def start_all(self) -> dict:
        with self._lock:
            self._assert_start_safe_unlocked()
            started = 0
            for profile in self.history.list_bot_profiles():
                if self._worker_factory is None and self._active_browser_count_unlocked():
                    break
                if self._active_browser_count_unlocked() >= self.max_workers:
                    break
                if self._effective_status_unlocked(profile["bot_id"], profile) in {
                    WorkerStatus.STOPPED, WorkerStatus.FAILED
                }:
                    self._start_bot_unlocked(profile["bot_id"])
                    started += 1
                    if self._worker_factory is None:
                        break
            self._log_unlocked("success" if started else "info",
                               f"Start All launched {started} bot(s)." if started else
                               "Start All found no eligible bot within the concurrency limit.")
            return self._snapshot_unlocked()

    def stop(self, worker_id: int) -> dict:
        with self._lock:
            if worker_id not in self._workers:
                raise KeyError(worker_id)
            return self.stop_bot(self._bot_id_for_worker_unlocked(worker_id))

    def stop_all(self, reason: str = "user_stop_all") -> dict:
        with self._lock:
            stopped = 0
            for bot_id, worker in list(self._current.items()):
                if worker.stop(reason):
                    stopped += 1
                    self.history.update_bot_profile(bot_id, status=WorkerStatus.STOPPING.value,
                                                    timestamp=utc_time())
            if stopped:
                self._log_unlocked("info", f"{reason}: stopping {stopped} bot(s).")
            controllers = list(self._external_controllers)
        for controller in controllers:
            stop = getattr(controller, "stop", None)
            if stop:
                stop(reason)
        with self._lock:
            return self._snapshot_unlocked()

    def shutdown(self, timeout: float = 12.0) -> None:
        self.stop_all("backend_shutdown")
        with self._lock:
            workers = list(self._current.values())
        deadline = time.monotonic() + timeout
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        for controller in list(self._external_controllers):
            join = getattr(controller, "join", None)
            if join:
                join(max(0.0, deadline - time.monotonic()))
        for profile in self.history.list_bot_profiles():
            self.history.update_bot_profile(profile["bot_id"], status=WorkerStatus.STOPPED.value,
                                            timestamp=utc_time())

    def update_safety(self, reason: str | None, stop_workers: bool = False,
                      latched: bool | None = None) -> bool:
        with self._lock:
            changed = reason != self._safety_reason
            self._safety_reason = reason
            if latched is not None:
                self._safety_latched = latched
            if changed:
                self._log_unlocked("danger" if reason else "success",
                                   reason or "Resources recovered; worker starts unlocked.")
        if stop_workers:
            self.stop_all(f"safety:{reason}")
        return changed

    @property
    def safety_latched(self) -> bool:
        with self._lock:
            return self._safety_latched

    def increase_limit(self) -> dict:
        with self._lock:
            self._assert_start_safe_unlocked()
            self.max_workers += 1
            self._log_unlocked("warning", f"User explicitly increased concurrency limit to {self.max_workers}.")
            return self._snapshot_unlocked()

    def register_external_controller(self, controller: object) -> None:
        with self._lock:
            if controller not in self._external_controllers:
                self._external_controllers.append(controller)

    def reserve_external_slot(self, slot_id: str) -> None:
        with self._lock:
            self._assert_start_safe_unlocked()
            if any(isinstance(worker, YouTubeWorker) and worker.status in WorkerStatus.active()
                   for worker in self._current.values()):
                raise ValueError("A selected-video bot is already active. Stop it before starting a diagnostic.")
            if slot_id in self._external_slots:
                raise ValueError("The diagnostic is already active.")
            if self._active_browser_count_unlocked() >= self.max_workers:
                raise ValueError(f"Maximum browser concurrency is {self.max_workers}.")
            self._external_slots.add(slot_id)
            self._log_unlocked("info", "Reserved one browser slot for a manual YouTube diagnostic.")

    def browser_start_eligibility(self) -> dict[str, object]:
        with self._lock:
            if any(isinstance(worker, YouTubeWorker) and worker.status in WorkerStatus.active()
                   for worker in self._current.values()):
                return {"allowed": False, "reason": "A selected-video bot is already active."}
            try:
                self._assert_start_safe_unlocked()
            except ValueError as error:
                return {"allowed": False, "reason": str(error)}
            active = self._active_browser_count_unlocked()
            if active >= self.max_workers:
                return {
                    "allowed": False,
                    "reason": (
                        f"Maximum browser concurrency is {self.max_workers}; "
                        f"stop a running browser first."
                    ),
                }
            return {"allowed": True, "reason": "Resources permit one diagnostic browser."}

    def record_external_memory(self, memory_mb: float) -> None:
        """Called by the resource monitor so diagnostic RAM is known without nested locks."""
        with self._lock:
            self._external_memory_mb = max(0.0, float(memory_mb or 0.0))
            self._observe_memory_unlocked(self._external_memory_mb)

    def release_external_slot(self, slot_id: str) -> None:
        with self._lock:
            if slot_id in self._external_slots:
                self._external_slots.remove(slot_id)
                self._log_unlocked("info", "Released the YouTube diagnostic browser slot.")

    def state(self) -> dict:
        with self._lock:
            return self._snapshot_unlocked()

    def logs(self, limit: int = 30) -> list[dict[str, str]]:
        with self._lock:
            return list(reversed(self._logs[-limit:]))

    def _start_bot_unlocked(self, bot_id: str) -> ManagedWorker:
        profile = self._profile_unlocked(bot_id)
        status = self._effective_status_unlocked(bot_id, profile)
        if status in WorkerStatus.active():
            raise ValueError(f"{profile['display_name']} is already {status.value.lower()}.")
        self._assert_start_safe_unlocked()
        if self._worker_factory is None and self._active_browser_count_unlocked():
            raise ValueError("Only one selected-video session can run at a time. Stop the active browser first.")
        if self._active_browser_count_unlocked() >= self.max_workers:
            raise ValueError(f"Maximum concurrency is {self.max_workers}.")
        worker_id = self._next_id
        self._next_id += 1
        worker = self._make_worker_unlocked(worker_id, bot_id, profile["display_name"])
        self._workers[worker_id] = worker
        self._current[bot_id] = worker
        self.history.update_bot_profile(bot_id, status=WorkerStatus.STARTING.value,
                                        timestamp=utc_time())
        if not worker.start():
            self.history.update_bot_profile(bot_id, status=WorkerStatus.FAILED.value,
                                            timestamp=utc_time())
            raise ValueError("The browser start request was rejected.")
        self.history.update_bot_profile(bot_id, status=worker.status.value, timestamp=utc_time())
        self._log_unlocked("success", f"{profile['display_name']} start requested as worker {worker_id}.")
        return worker

    @staticmethod
    def _validate_settings(settings: dict | None) -> None:
        if settings is not None and settings.get("playback_mode", "repeat") not in ("repeat", "once"):
            raise ValueError("Playback mode must be repeat or once.")

    def _make_worker_unlocked(self, worker_id: int, bot_id: str, bot_name: str) -> ManagedWorker:
        if self._worker_factory is None:
            selection = self.history.get_youtube_selection()
            if selection is None:
                raise ValueError("Select and save a YouTube video before starting a bot.")
            return YouTubeWorker(worker_id, selection, self.history, session_id=self.session_id,
                                 bot_id=bot_id, bot_name=bot_name, on_terminal=self._on_terminal)
        parameters = inspect.signature(self._worker_factory).parameters
        worker = (self._worker_factory(worker_id, bot_id, bot_name)
                  if len(parameters) >= 3 else self._worker_factory(worker_id))
        setattr(worker, "bot_id", getattr(worker, "bot_id", bot_id))
        setattr(worker, "bot_name", getattr(worker, "bot_name", bot_name))
        return worker

    def _on_terminal(self, bot_id: str, status: WorkerStatus) -> None:
        self.history.update_bot_profile(bot_id, status=status.value, timestamp=utc_time())

    def _assert_start_safe_unlocked(self) -> None:
        if self._safety_reason or self._safety_latched:
            raise ValueError(self._safety_reason or "Worker starts remain locked until resources recover.")
        memory = self.metrics_provider.virtual_memory()
        ram, available = float(memory.percent), float(memory.available) / 1024**3
        if ram >= RAM_START_BLOCK_PERCENT:
            raise ValueError(f"Worker start blocked: RAM is {ram:.1f}% (limit {RAM_START_BLOCK_PERCENT:.0f}%).")
        if available < MIN_AVAILABLE_RAM_GB:
            raise ValueError(f"Worker start blocked: {available:.2f} GB RAM is available; at least {MIN_AVAILABLE_RAM_GB:.2f} GB is required.")
        if self._memory_capacity_unlocked() < 1:
            raise ValueError(
                f"Memory limit reached: another bot needs about {self._ram_estimate_mb:.0f} MB, which would "
                f"exceed the {RAM_START_BLOCK_PERCENT:.0f}% RAM limit or leave less than "
                f"{MIN_AVAILABLE_RAM_GB:.2f} GB free. Can't start more bots."
            )

    def _observe_memory_unlocked(self, memory_mb: float) -> None:
        # The estimate only grows: Chrome's footprint rises during playback, so the peak is the safe value.
        if memory_mb > self._ram_estimate_mb:
            self._ram_estimate_mb = memory_mb

    def _memory_capacity_unlocked(self) -> int:
        """How many more bots fit before the start-block RAM limit or the free-RAM floor."""
        memory = self.metrics_provider.virtual_memory()
        total_mb = float(memory.total) / 1024**2
        available_mb = float(memory.available) / 1024**2
        estimate = self._ram_estimate_mb
        # Browsers that are still starting have not allocated their full footprint yet.
        pending = 0.0
        for worker in self._current.values():
            if worker.status in WorkerStatus.active():
                used = float(worker.snapshot().get("process", {}).get("memory_mb", 0.0) or 0.0)
                pending += max(0.0, estimate - used)
        if self._external_slots:
            pending += max(0.0, estimate - self._external_memory_mb)
        headroom_percent = total_mb * RAM_START_BLOCK_PERCENT / 100 - (total_mb - available_mb + pending)
        headroom_available = available_mb - pending - MIN_AVAILABLE_RAM_GB * 1024
        # Projected RAM must stay strictly below the start limit; the free-RAM floor may be met exactly.
        by_percent = math.ceil(headroom_percent / estimate) - 1
        by_available = math.floor(headroom_available / estimate)
        return max(0, min(by_percent, by_available))

    def _capacity_unlocked(self) -> dict:
        memory_bots = self._memory_capacity_unlocked()
        active = self._active_browser_count_unlocked()
        slots = max(0, self.max_workers - active)
        if self._worker_factory is None and active:
            slots = 0
        more_bots = min(memory_bots, slots)
        if self._safety_reason or self._safety_latched:
            message = self._safety_reason or "Worker starts remain locked until resources recover."
        elif memory_bots < 1:
            message = "Memory limit reached - can't start more bots."
        elif more_bots < 1:
            message = ("Only one selected-video bot can run at a time." if self._worker_factory is None
                       else f"Concurrency limit of {self.max_workers} reached.")
        else:
            message = f"You can run about {more_bots} more bot{'s' if more_bots != 1 else ''}."
        return {"memory_bots": memory_bots, "more_bots": more_bots,
                "estimate_mb": round(self._ram_estimate_mb, 1),
                "memory_limited": memory_bots < 1, "message": message}

    def _profile_unlocked(self, bot_id: str) -> dict:
        profile = self.history.get_bot_profile(bot_id)
        if profile is None:
            raise KeyError(bot_id)
        return profile

    def _effective_status_unlocked(self, bot_id: str, profile: dict) -> WorkerStatus:
        worker = self._current.get(bot_id)
        status = worker.status if worker else WorkerStatus(profile["last_status"])
        if worker and profile["last_status"] != status.value:
            self.history.update_bot_profile(bot_id, status=status.value, timestamp=utc_time())
        return status

    def _bot_id_for_worker_unlocked(self, worker_id: int) -> str:
        for bot_id, worker in self._current.items():
            if worker.id == worker_id:
                return bot_id
        raise KeyError(worker_id)

    def _active_count_unlocked(self) -> int:
        return sum(w.status in WorkerStatus.active() for w in self._current.values())

    def _active_browser_count_unlocked(self) -> int:
        return self._active_count_unlocked() + len(self._external_slots)

    def _bot_snapshot_unlocked(self, profile: dict) -> dict:
        bot_id = profile["bot_id"]
        worker = self._current.get(bot_id)
        status = self._effective_status_unlocked(bot_id, profile)
        run = worker.snapshot() if worker else None
        last_run = self.history.last_run_for_bot(bot_id)
        process = (run or {}).get("process", {"driver_pid": None, "pids": [], "memory_mb": 0.0})
        self._observe_memory_unlocked(float(process.get("memory_mb", 0.0) or 0.0))
        return {**profile, "status": status.value, "worker_id": worker.id if worker else None,
                "video_id": (run or {}).get("video_id"),
                "video_title": (run or {}).get("video_title"),
                "playback_mode": "once" if self._worker_factory is None else profile["settings"].get("playback_mode", "repeat"),
                "run_id": (run or {}).get("run_id"), "process": process,
                "browser_memory_mb": process.get("memory_mb", 0.0),
                "playback_state": (run or {}).get("playback_state", "not started"),
                "progress_percent": (run or {}).get("progress_percent", 0.0),
                "elapsed_seconds": (run or {}).get("elapsed_seconds", 0.0),
                "last_execution_duration": last_run.get("execution_duration", 0.0) if last_run else 0.0,
                "last_error": (run or {}).get("last_error") or
                              (last_run.get("error_message") if last_run else None),
                "stop_reason": (run or {}).get("stop_reason")}

    def _snapshot_unlocked(self) -> dict:
        bots = [self._bot_snapshot_unlocked(p) for p in self.history.list_bot_profiles()]
        workers = [worker.snapshot() for worker in self._workers.values()]
        active_values = {status.value for status in WorkerStatus.active()}
        counts = {status.value: sum(bot["status"] == status.value for bot in bots)
                  for status in WorkerStatus}
        return {"count": sum(bot["status"] in active_values for bot in bots),
                "total_records": len(workers), "total_saved": len(bots),
                "running_bots": counts[WorkerStatus.RUNNING.value] + counts[WorkerStatus.STARTING.value],
                "stopped_bots": counts[WorkerStatus.STOPPED.value],
                "failed_bots": counts[WorkerStatus.FAILED.value],
                "target_workers": self.target_workers, "max_workers": self.max_workers,
                "total_active_browsers": self._active_browser_count_unlocked(),
                "external_active": len(self._external_slots),
                "safety_locked": self._safety_reason is not None or self._safety_latched,
                "safety_reason": self._safety_reason, "bots": bots,
                "capacity": self._capacity_unlocked(),
                "workers": list(reversed(workers))}

    def _log_unlocked(self, level: str, message: str) -> None:
        self._logs.append({"time": utc_time(), "level": level, "message": message})
        self._logs = self._logs[-200:]
