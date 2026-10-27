from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable


FINAL_STATUSES = {"completed", "failed", "stopped", "safety_stopped"}
SORT_COLUMNS = {
    "started_at": "started_at",
    "ended_at": "ended_at",
    "duration": "execution_duration",
    "status": "final_status",
    "worker_id": "worker_id",
    "progress": "playback_progress",
}


class JobHistory:
    """Thread-safe SQLite repository for legacy jobs and Phase 3 analytics."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._write_lock = RLock()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    test_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('completed', 'failed')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    details TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    app_version TEXT NOT NULL DEFAULT '3.0.0'
                );

                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_profiles (
                    bot_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    settings_json TEXT NOT NULL DEFAULT '{}',
                    last_status TEXT NOT NULL DEFAULT 'Stopped'
                        CHECK(last_status IN ('Stopped','Starting','Running','Stopping','Failed'))
                );

                CREATE TABLE IF NOT EXISTS youtube_selection (
                    selection_id INTEGER PRIMARY KEY CHECK(selection_id = 1),
                    channel_id TEXT NOT NULL,
                    channel_title TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    video_title TEXT NOT NULL,
                    video_url TEXT NOT NULL,
                    thumbnail_url TEXT,
                    duration TEXT NOT NULL DEFAULT 'PT0S',
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    published_at TEXT,
                    statistics_json TEXT NOT NULL DEFAULT '{}',
                    selected_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS youtube_diagnostics (
                    diagnostic_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    elapsed_seconds REAL NOT NULL DEFAULT 0,
                    playback_seconds REAL NOT NULL DEFAULT 0,
                    playback_status TEXT NOT NULL DEFAULT 'not started',
                    error_message TEXT,
                    process_info TEXT NOT NULL DEFAULT '{}',
                    stop_reason TEXT,
                    cleanup_verified INTEGER,
                    orphaned_pids_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS test_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    worker_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    execution_duration REAL NOT NULL DEFAULT 0,
                    final_status TEXT CHECK(final_status IN ('completed','failed','stopped','safety_stopped')),
                    playback_duration REAL NOT NULL DEFAULT 0,
                    playback_progress REAL NOT NULL DEFAULT 0,
                    error_message TEXT,
                    browser_process_info TEXT NOT NULL DEFAULT '{}',
                    stop_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS worker_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES test_runs(run_id) ON DELETE CASCADE,
                    worker_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    details TEXT,
                    UNIQUE(run_id, state)
                );

                CREATE TABLE IF NOT EXISTS resource_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    timestamp TEXT NOT NULL,
                    cpu_percent REAL NOT NULL,
                    ram_percent REAL NOT NULL,
                    available_ram_gb REAL NOT NULL,
                    worker_ram_mb REAL NOT NULL,
                    active_worker_count INTEGER NOT NULL,
                    worker_memory_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_runs_started ON test_runs(started_at);
                CREATE INDEX IF NOT EXISTS idx_runs_session ON test_runs(session_id);
                CREATE INDEX IF NOT EXISTS idx_runs_worker ON test_runs(worker_id);
                CREATE INDEX IF NOT EXISTS idx_runs_status ON test_runs(final_status);
                CREATE INDEX IF NOT EXISTS idx_transitions_run ON worker_transitions(run_id);
                CREATE INDEX IF NOT EXISTS idx_resources_timestamp ON resource_samples(timestamp);
                CREATE INDEX IF NOT EXISTS idx_resources_session ON resource_samples(session_id);
                CREATE INDEX IF NOT EXISTS idx_youtube_diagnostics_started
                    ON youtube_diagnostics(started_at);
                CREATE INDEX IF NOT EXISTS idx_youtube_diagnostics_video_started
                    ON youtube_diagnostics(video_id, started_at DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(test_runs)").fetchall()
            }
            if "bot_id" not in columns:
                connection.execute("ALTER TABLE test_runs ADD COLUMN bot_id TEXT")
            if "bot_name" not in columns:
                connection.execute("ALTER TABLE test_runs ADD COLUMN bot_name TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_runs_bot ON test_runs(bot_id)")
            resource_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(resource_samples)").fetchall()
            }
            for name in (
                "backend_rss_mb", "chrome_memory_mb", "chromedriver_memory_mb",
                "youtube_memory_mb",
            ):
                if name not in resource_columns:
                    connection.execute(
                        f"ALTER TABLE resource_samples ADD COLUMN {name} REAL NOT NULL DEFAULT 0"
                    )
            diagnostic_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(youtube_diagnostics)").fetchall()
            }
            diagnostic_migrations = {
                "elapsed_seconds": "REAL NOT NULL DEFAULT 0",
                "playback_status": "TEXT NOT NULL DEFAULT 'not started'",
                "stop_reason": "TEXT",
                "cleanup_verified": "INTEGER",
                "orphaned_pids_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for name, declaration in diagnostic_migrations.items():
                if name not in diagnostic_columns:
                    connection.execute(
                        f"ALTER TABLE youtube_diagnostics ADD COLUMN {name} {declaration}"
                    )

    # Persistent bot profiles ----------------------------------------------
    def seed_default_profiles(self, count: int, timestamp: str) -> int:
        """Create the initial stopped profiles once; deleting all bots stays persistent."""
        with self._write_lock, self._connect() as connection:
            marker = connection.execute(
                "SELECT value FROM app_metadata WHERE key='phase4_profiles_seeded'"
            ).fetchone()
            if marker is not None:
                return 0
            existing = int(connection.execute("SELECT COUNT(*) FROM bot_profiles").fetchone()[0])
            created = 0
            if existing == 0:
                for number in range(1, max(0, count) + 1):
                    connection.execute(
                        """
                        INSERT INTO bot_profiles(
                            bot_id,display_name,created_at,updated_at,settings_json,last_status
                        ) VALUES (?,?,?,?,?,'Stopped')
                        """,
                        (str(uuid.uuid4()), f"Bot {number:03d}", timestamp, timestamp, "{}"),
                    )
                    created += 1
            connection.execute(
                "INSERT INTO app_metadata(key,value) VALUES('phase4_profiles_seeded','1')"
            )
            return created

    def restore_profiles_stopped(self, timestamp: str) -> int:
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE bot_profiles SET last_status='Stopped',updated_at=? WHERE last_status<>'Stopped'",
                (timestamp,),
            )
            return cursor.rowcount

    def create_bot_profile(
        self, bot_id: str, display_name: str, created_at: str, settings: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_profiles(
                    bot_id,display_name,created_at,updated_at,settings_json,last_status
                ) VALUES (?,?,?,?,?,'Stopped')
                """,
                (bot_id, display_name, created_at, created_at, json.dumps(settings or {})),
            )
        return self.get_bot_profile(bot_id)  # type: ignore[return-value]

    def list_bot_profiles(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bot_profiles ORDER BY created_at, bot_id"
            ).fetchall()
        return [self._decode_profile(row) for row in rows]

    def get_bot_profile(self, bot_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bot_profiles WHERE bot_id=?", (bot_id,)
            ).fetchone()
        return self._decode_profile(row) if row else None

    def update_bot_profile(
        self, bot_id: str, *, display_name: str | None = None,
        settings: dict[str, Any] | None = None, status: str | None = None,
        timestamp: str,
    ) -> bool:
        assignments, params = ["updated_at=?"], [timestamp]
        if display_name is not None:
            assignments.append("display_name=?")
            params.append(display_name)
        if settings is not None:
            assignments.append("settings_json=?")
            params.append(json.dumps(settings, separators=(",", ":")))
        if status is not None:
            assignments.append("last_status=?")
            params.append(status)
        params.append(bot_id)
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE bot_profiles SET {', '.join(assignments)} WHERE bot_id=?", params
            )
            return cursor.rowcount == 1

    def delete_bot_profile(self, bot_id: str) -> bool:
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM bot_profiles WHERE bot_id=?", (bot_id,))
            return cursor.rowcount == 1

    def next_worker_id(self) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COALESCE(MAX(worker_id),0)+1 FROM test_runs"
            ).fetchone()[0])

    def last_run_for_bot(self, bot_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM test_runs WHERE bot_id=? ORDER BY started_at DESC,run_id DESC LIMIT 1",
                (bot_id,),
            ).fetchone()
        return self._decode_run(row) if row else None

    # YouTube selection and isolated diagnostics ---------------------------
    def save_youtube_selection(self, channel: dict[str, Any], video: dict[str, Any],
                               timestamp: str) -> dict[str, Any]:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO youtube_selection(
                    selection_id,channel_id,channel_title,video_id,video_title,video_url,
                    thumbnail_url,duration,duration_seconds,published_at,statistics_json,
                    selected_at,updated_at
                ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(selection_id) DO UPDATE SET
                    channel_id=excluded.channel_id,channel_title=excluded.channel_title,
                    video_id=excluded.video_id,video_title=excluded.video_title,
                    video_url=excluded.video_url,thumbnail_url=excluded.thumbnail_url,
                    duration=excluded.duration,duration_seconds=excluded.duration_seconds,
                    published_at=excluded.published_at,
                    statistics_json=excluded.statistics_json,updated_at=excluded.updated_at
                """,
                (
                    video["channel_id"], channel.get("title") or video.get("channel_title") or "",
                    video["video_id"], video["title"], video["url"], video.get("thumbnail_url"),
                    video.get("duration", "PT0S"), int(video.get("duration_seconds", 0)),
                    video.get("published_at"), json.dumps(video.get("statistics", {})),
                    timestamp, timestamp,
                ),
            )
        return self.get_youtube_selection()  # type: ignore[return-value]

    def get_youtube_selection(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM youtube_selection WHERE selection_id=1").fetchone()
        if row is None:
            return None
        result = dict(row)
        result["statistics"] = json.loads(result.pop("statistics_json") or "{}")
        return result

    def start_youtube_diagnostic(self, diagnostic_id: str, video_id: str,
                                 started_at: str) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO youtube_diagnostics(diagnostic_id,video_id,started_at,status)
                VALUES(?,?,?,'Starting')
                """, (diagnostic_id, video_id, started_at)
            )

    def finish_youtube_diagnostic(
        self, diagnostic_id: str, *, ended_at: str, status: str,
        elapsed_seconds: float, playback_seconds: float, playback_status: str,
        error_message: str | None, process_info: dict[str, Any], stop_reason: str | None,
        cleanup_verified: bool | None, orphaned_pids: list[int],
    ) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE youtube_diagnostics SET ended_at=?,status=?,elapsed_seconds=?,
                    playback_seconds=?,playback_status=?,error_message=?,process_info=?,
                    stop_reason=?,cleanup_verified=?,orphaned_pids_json=?
                WHERE diagnostic_id=? AND ended_at IS NULL
                """,
                (
                    ended_at, status, elapsed_seconds, playback_seconds, playback_status,
                    error_message, json.dumps(process_info, separators=(",", ":")),
                    stop_reason, None if cleanup_verified is None else int(cleanup_verified),
                    json.dumps(orphaned_pids), diagnostic_id,
                ),
            )

    def latest_youtube_diagnostic(self, video_id: str | None = None) -> dict[str, Any] | None:
        where, params = "", []
        if video_id:
            where, params = "WHERE video_id=?", [video_id]
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM youtube_diagnostics {where} ORDER BY started_at DESC LIMIT 1",
                params,
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["process"] = json.loads(result.pop("process_info") or "{}")
        result["orphaned_pids"] = json.loads(result.pop("orphaned_pids_json") or "[]")
        if result.get("cleanup_verified") is not None:
            result["cleanup_verified"] = bool(result["cleanup_verified"])
        return result

    def list_youtube_diagnostics(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM youtube_diagnostics ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["process"] = json.loads(item.pop("process_info") or "{}")
            item["orphaned_pids"] = json.loads(item.pop("orphaned_pids_json") or "[]")
            if item.get("cleanup_verified") is not None:
                item["cleanup_verified"] = bool(item["cleanup_verified"])
            items.append(item)
        return items

    # Phase 1/2 compatibility -------------------------------------------------
    def add(self, test_name: str, status: str, details: str = "") -> int:
        if status not in {"completed", "failed"}:
            raise ValueError("status must be 'completed' or 'failed'")
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO jobs (test_name, status, details) VALUES (?, ?, ?)",
                (test_name, status, details),
            )
            return int(cursor.lastrowid)

    def counts(self) -> dict[str, int]:
        result = {"completed": 0, "failed": 0}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, SUM(count) AS count FROM (
                    SELECT status, COUNT(*) AS count FROM jobs GROUP BY status
                    UNION ALL
                    SELECT final_status AS status, COUNT(*) AS count
                    FROM test_runs WHERE final_status IN ('completed','failed') GROUP BY final_status
                ) GROUP BY status
                """
            ).fetchall()
        for row in rows:
            result[str(row["status"])] = int(row["count"])
        return result

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, test_name, status, created_at, details FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # Sessions and execution history ----------------------------------------
    def start_session(self, session_id: str, started_at: str) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE test_runs SET ended_at=?,
                    execution_duration=MAX(0, (julianday(?) - julianday(started_at)) * 86400),
                    final_status='failed', error_message=COALESCE(error_message, ?),
                    stop_reason=COALESCE(stop_reason, 'unclean_shutdown')
                WHERE ended_at IS NULL AND session_id <> ?
                """,
                (started_at, started_at, "Backend ended before the run was finalized.", session_id),
            )
            connection.execute(
                "UPDATE sessions SET ended_at=? WHERE ended_at IS NULL AND session_id <> ?",
                (started_at, session_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO sessions(session_id, started_at, app_version) VALUES (?, ?, '5.0.0')",
                (session_id, started_at),
            )

    def end_session(self, session_id: str, ended_at: str) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET ended_at = COALESCE(ended_at, ?) WHERE session_id = ?",
                (ended_at, session_id),
            )

    def begin_run(
        self, run_id: str, session_id: str, worker_id: int, started_at: str,
        bot_id: str | None = None, bot_name: str | None = None,
    ) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO test_runs(
                    run_id, session_id, worker_id, started_at, bot_id, bot_name
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, session_id, worker_id, started_at, bot_id, bot_name),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO worker_transitions(run_id, worker_id, state, timestamp)
                VALUES (?, ?, 'Starting', ?)
                """,
                (run_id, worker_id, started_at),
            )

    def record_transition(
        self, run_id: str, worker_id: int, state: str, timestamp: str, details: str | None = None
    ) -> bool:
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO worker_transitions(run_id, worker_id, state, timestamp, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, worker_id, state, timestamp, details),
            )
            return cursor.rowcount == 1

    def finalize_run(
        self,
        run_id: str,
        *,
        ended_at: str,
        execution_duration: float,
        final_status: str,
        playback_duration: float,
        playback_progress: float,
        error_message: str | None,
        browser_process_info: dict,
        stop_reason: str | None,
    ) -> bool:
        if final_status not in FINAL_STATUSES:
            raise ValueError(f"Unsupported final status: {final_status}")
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE test_runs SET ended_at=?, execution_duration=?, final_status=?,
                    playback_duration=?, playback_progress=?, error_message=?,
                    browser_process_info=?, stop_reason=?
                WHERE run_id=? AND ended_at IS NULL
                """,
                (
                    ended_at,
                    float(execution_duration),
                    final_status,
                    float(playback_duration),
                    float(playback_progress),
                    error_message,
                    json.dumps(browser_process_info, separators=(",", ":")),
                    stop_reason,
                    run_id,
                ),
            )
            return cursor.rowcount == 1

    # Buffered resource samples ---------------------------------------------
    def insert_resource_samples(self, samples: Iterable[dict[str, Any]]) -> int:
        rows = list(samples)
        if not rows:
            return 0
        values = [
            (
                row["session_id"], row["timestamp"], row["cpu_percent"], row["ram_percent"],
                row["available_ram_gb"], row["worker_ram_mb"], row["active_worker_count"],
                json.dumps(row.get("worker_memory", {}), separators=(",", ":")),
                row.get("backend_rss_mb", 0), row.get("chrome_memory_mb", 0),
                row.get("chromedriver_memory_mb", 0), row.get("youtube_memory_mb", 0),
            )
            for row in rows
        ]
        with self._write_lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO resource_samples(
                    session_id,timestamp,cpu_percent,ram_percent,available_ram_gb,
                    worker_ram_mb,active_worker_count,worker_memory_json
                    ,backend_rss_mb,chrome_memory_mb,chromedriver_memory_mb,youtube_memory_mb
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
        return len(values)

    # Analytics queries ------------------------------------------------------
    def analytics_summary(self, filters: dict[str, Any]) -> dict[str, Any]:
        where, params = self._run_where(filters)
        resource_where, resource_params = self._resource_where(filters)
        with self._connect() as connection:
            run = connection.execute(
                f"""
                SELECT COUNT(*) total_runs,
                    SUM(CASE WHEN final_status='completed' THEN 1 ELSE 0 END) successful_runs,
                    SUM(CASE WHEN final_status='failed' THEN 1 ELSE 0 END) failed_runs,
                    SUM(CASE WHEN final_status='safety_stopped' THEN 1 ELSE 0 END) safety_shutdowns,
                    AVG(execution_duration) average_duration,
                    MAX(execution_duration) maximum_duration
                FROM test_runs {where}
                """,
                params,
            ).fetchone()
            resource = connection.execute(
                f"""
                SELECT MAX(cpu_percent) peak_cpu, MAX(ram_percent) peak_ram,
                       MAX(worker_ram_mb) peak_worker_ram,
                       MAX(backend_rss_mb) peak_backend_rss,
                       MAX(chrome_memory_mb) peak_chrome_ram,
                       MAX(chromedriver_memory_mb) peak_chromedriver_ram
                FROM resource_samples {resource_where}
                """,
                resource_params,
            ).fetchone()
            frequency = connection.execute(
                f"""
                SELECT worker_id, COUNT(*) failures FROM test_runs {where}
                {"AND" if where else "WHERE"} final_status='failed'
                GROUP BY worker_id ORDER BY failures DESC, worker_id ASC
                """,
                params,
            ).fetchall()
        total = int(run["total_runs"] or 0)
        successful = int(run["successful_runs"] or 0)
        return {
            "total_runs": total,
            "successful_runs": successful,
            "failed_runs": int(run["failed_runs"] or 0),
            "success_percentage": round(successful / total * 100, 2) if total else 0.0,
            "average_duration": round(float(run["average_duration"] or 0), 2),
            "maximum_duration": round(float(run["maximum_duration"] or 0), 2),
            "peak_cpu": round(float(resource["peak_cpu"] or 0), 2),
            "peak_ram": round(float(resource["peak_ram"] or 0), 2),
            "peak_worker_ram": round(float(resource["peak_worker_ram"] or 0), 2),
            "peak_backend_rss": round(float(resource["peak_backend_rss"] or 0), 2),
            "peak_chrome_ram": round(float(resource["peak_chrome_ram"] or 0), 2),
            "peak_chromedriver_ram": round(float(resource["peak_chromedriver_ram"] or 0), 2),
            "safety_shutdowns": int(run["safety_shutdowns"] or 0),
            "worker_failure_frequency": [dict(row) for row in frequency],
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.session_id,s.started_at,s.ended_at,COUNT(r.run_id) run_count
                FROM sessions s LEFT JOIN test_runs r ON r.session_id=s.session_id
                GROUP BY s.session_id ORDER BY s.started_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_runs(
        self, filters: dict[str, Any], page: int = 1, page_size: int = 25,
        sort_by: str = "started_at", sort_order: str = "desc",
    ) -> dict[str, Any]:
        where, params = self._run_where(filters)
        column = SORT_COLUMNS.get(sort_by, "started_at")
        direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        offset = (page - 1) * page_size
        with self._connect() as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM test_runs {where}", params
            ).fetchone()[0])
            rows = connection.execute(
                f"""
                SELECT run_id,session_id,worker_id,bot_id,bot_name,started_at,ended_at,execution_duration,
                       final_status,playback_duration,playback_progress,error_message,
                       browser_process_info,stop_reason
                FROM test_runs {where} ORDER BY {column} {direction}, run_id {direction}
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        return {
            "items": [self._decode_run(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
        }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM test_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            transitions = connection.execute(
                "SELECT state,timestamp,details FROM worker_transitions WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        result = self._decode_run(row)
        result["transitions"] = [dict(item) for item in transitions]
        return result

    def list_resources(self, filters: dict[str, Any], limit: int = 1000) -> list[dict[str, Any]]:
        where, params = self._resource_where(filters)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM (
                SELECT session_id,timestamp,cpu_percent,ram_percent,available_ram_gb,
                       worker_ram_mb,active_worker_count,worker_memory_json,backend_rss_mb,
                       chrome_memory_mb,chromedriver_memory_mb,youtube_memory_mb
                FROM resource_samples {where} ORDER BY timestamp DESC LIMIT ?
                ) ORDER BY timestamp ASC
                """,
                [*params, limit],
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["worker_memory"] = json.loads(item.pop("worker_memory_json") or "{}")
            result.append(item)
        return result

    def list_errors(self, filters: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
        where, params = self._run_where(filters)
        connector = "AND" if where else "WHERE"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT run_id,session_id,worker_id,bot_id,bot_name,started_at,ended_at,error_message
                FROM test_runs {where} {connector} error_message IS NOT NULL
                ORDER BY started_at DESC LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def cleanup_retention(
        self, active_session_id: str, resource_days: int = 7, run_days: int = 90,
        now: datetime | None = None,
    ) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        resource_cutoff = (now - timedelta(days=resource_days)).isoformat(timespec="seconds")
        run_cutoff = (now - timedelta(days=run_days)).isoformat(timespec="seconds")
        with self._write_lock, self._connect() as connection:
            resources = connection.execute(
                "DELETE FROM resource_samples WHERE timestamp < ? AND session_id <> ?",
                (resource_cutoff, active_session_id),
            ).rowcount
            runs = connection.execute(
                """
                DELETE FROM test_runs
                WHERE ended_at IS NOT NULL AND ended_at < ? AND session_id <> ?
                """,
                (run_cutoff, active_session_id),
            ).rowcount
        return {"resource_samples": resources, "test_runs": runs}

    def _run_where(self, filters: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if filters.get("start"):
            clauses.append("started_at >= ?")
            params.append(filters["start"])
        if filters.get("end"):
            clauses.append("started_at <= ?")
            params.append(filters["end"])
        if filters.get("session_id"):
            clauses.append("session_id = ?")
            params.append(filters["session_id"])
        if filters.get("worker_id") is not None:
            clauses.append("worker_id = ?")
            params.append(filters["worker_id"])
        if filters.get("bot_id"):
            clauses.append("bot_id = ?")
            params.append(filters["bot_id"])
        if filters.get("status"):
            clauses.append("final_status = ?")
            params.append(filters["status"])
        return ("WHERE " + " AND ".join(clauses) if clauses else "", params)

    def _resource_where(self, filters: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if filters.get("start"):
            clauses.append("timestamp >= ?")
            params.append(filters["start"])
        if filters.get("end"):
            clauses.append("timestamp <= ?")
            params.append(filters["end"])
        if filters.get("session_id"):
            clauses.append("session_id = ?")
            params.append(filters["session_id"])
        return ("WHERE " + " AND ".join(clauses) if clauses else "", params)

    @staticmethod
    def _decode_run(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["browser_process_info"] = json.loads(result.get("browser_process_info") or "{}")
        return result

    @staticmethod
    def _decode_profile(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["settings"] = json.loads(result.pop("settings_json") or "{}")
        return result

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection
