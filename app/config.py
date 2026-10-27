import os
from pathlib import Path


def read_youtube_api_key() -> str:
    """Read the process value first, then the persistent Windows user value."""
    value = os.getenv("YOUTUBE_API_KEY", "").strip()
    if value or os.name != "nt":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            stored, _ = winreg.QueryValueEx(key, "YOUTUBE_API_KEY")
        return str(stored).strip()
    except (FileNotFoundError, OSError, TypeError):
        return ""


BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_PATH = BASE_DIR / "data" / "dashboard.db"

HOST = "127.0.0.1"
PORT = 8000

INITIAL_WORKERS = max(0, int(os.getenv("PLAYBACK_TARGET_WORKERS", "1")))
MAX_WORKERS = max(1, int(os.getenv("PLAYBACK_MAX_WORKERS", "1")))
RAM_START_BLOCK_PERCENT = float(os.getenv("PLAYBACK_RAM_START_PERCENT", "84"))
RAM_WARNING_PERCENT = RAM_START_BLOCK_PERCENT
RAM_SHUTDOWN_PERCENT = float(os.getenv("PLAYBACK_RAM_SHUTDOWN_PERCENT", "95"))
MIN_AVAILABLE_RAM_GB = float(os.getenv("PLAYBACK_MIN_AVAILABLE_RAM_GB", "1.0"))
SAFETY_HYSTERESIS_PERCENT = float(os.getenv("PLAYBACK_SAFETY_HYSTERESIS_PERCENT", "5"))
AVAILABLE_RAM_HYSTERESIS_GB = float(os.getenv("PLAYBACK_AVAILABLE_HYSTERESIS_GB", "0.25"))
# Starting RAM estimate per bot (Chrome + ChromeDriver); raised to the observed peak at runtime.
BOT_RAM_ESTIMATE_MB = max(1.0, float(os.getenv("PLAYBACK_BOT_RAM_ESTIMATE_MB", "740")))
CPU_SUSTAINED_PERCENT = float(os.getenv("PLAYBACK_CPU_LIMIT_PERCENT", "90"))
CPU_SUSTAINED_SAMPLES = max(1, int(os.getenv("PLAYBACK_CPU_SAMPLES", "5")))
METRIC_INTERVAL_SECONDS = 2
HEADLESS_CHROME = os.getenv("PLAYBACK_HEADLESS", "true").lower() not in {"0", "false", "no"}
RESOURCE_RETENTION_DAYS = max(1, int(os.getenv("RESOURCE_RETENTION_DAYS", "7")))
EXECUTION_RETENTION_DAYS = max(1, int(os.getenv("EXECUTION_RETENTION_DAYS", "90")))
RESOURCE_BUFFER_SIZE = max(10, int(os.getenv("RESOURCE_BUFFER_SIZE", "300")))
RESOURCE_BATCH_SIZE = max(1, int(os.getenv("RESOURCE_BATCH_SIZE", "15")))

PLAYBACK_URL = f"http://{HOST}:{PORT}/playback-test"

YOUTUBE_API_KEY = read_youtube_api_key()
YOUTUBE_API_TIMEOUT_SECONDS = float(os.getenv("YOUTUBE_API_TIMEOUT_SECONDS", "10"))
YOUTUBE_MAX_RESULTS = max(1, min(50, int(os.getenv("YOUTUBE_MAX_RESULTS", "24"))))
