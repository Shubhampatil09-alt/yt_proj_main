from datetime import datetime, timezone
from enum import Enum


def utc_time() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkerStatus(str, Enum):
    STARTING = "Starting"
    RUNNING = "Running"
    STOPPING = "Stopping"
    STOPPED = "Stopped"
    FAILED = "Failed"

    @classmethod
    def active(cls) -> set["WorkerStatus"]:
        return {cls.STARTING, cls.RUNNING, cls.STOPPING}

