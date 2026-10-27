# Local Bot Manager - Phase 6

A locally hosted FastAPI dashboard with saved bot profiles. Press **Start** on a bot to play the selected YouTube video once in visible Chrome. Only one YouTube playback session may run at a time, including the separate manual diagnostic.

## Phase 6 release highlights

- Guided YouTube flow: **Search Channel -> Select Video -> Run Diagnostic -> View Results**
- Prominent persisted selected-video card with ownership-confirmed title, thumbnail, channel, duration, and URL
- In-page **Change Video** action and paginated channel uploads without a page refresh
- One explicit, single-session diagnostic with Start/Stop controls and Starting, Running, Stopping, Completed, Failed, Stopped, and Safety Stopped states
- Actionable diagnostic blocking reasons from the backend resource gate (RAM, available memory, sustained CPU, concurrency, or active diagnostic)
- Persisted diagnostic elapsed time, playback state, stop reason, errors, process snapshot, and cleanup verification
- Results summary with an Analytics & Reports link
- Separate system RAM, FastAPI RSS, managed Chrome RAM, and ChromeDriver RAM display
- A guarded Windows launcher with dependency/configuration checks and duplicate-instance detection
- Dashboard polling remains one consolidated request every two seconds and pauses while the tab is hidden

Nothing starts on application launch or video selection. **Start Bot** plays the SQLite-selected video once through the shared YouTube iframe player. The toolbar's **Start One Bot** action starts at most one eligible profile. Playback ends on video completion, Stop, or a safety limit; there is no time limit, automatic repeat, or automatic restart. The selected video is fixed for each execution even if the dashboard selection changes while it runs.

## Phase 5 YouTube discovery and diagnostics

- Server-side integration with the official YouTube Data API v3
- Search by channel name, `@handle`, channel ID, or `youtube.com` channel URL
- Channel thumbnails and public channel statistics
- Recent uploads read from the channel's official uploads playlist, with titles, thumbnails, durations, dates, and public statistics
- Exactly one persisted target selection; every selection is re-fetched and checked against `snippet.channelId`
- API key read only from `YOUTUBE_API_KEY`; it is never returned to JavaScript or written to logs
- Explicit handling for missing configuration, rejected credentials, quota exhaustion, unavailable videos, and ownership mismatches
- One manually initiated, single-session playback diagnostic using a `youtube-nocookie.com` embed
- The diagnostic shares the global resource/concurrency gate, participates in emergency shutdown, and cannot be launched by Start All
- Saved bots now use the selected YouTube video; the local HTML5 fixture remains available for regression testing only

## Phase 5 performance work

- FastAPI process RSS profiling through `/api/performance`
- Separate Chrome and ChromeDriver process-tree memory accounting
- PID plus process-creation-time cleanup validation and leak warnings
- One consolidated dashboard polling endpoint instead of three requests every two seconds
- Browser polling pauses while the dashboard tab is hidden
- Resource samples retain two-second collection with bounded, batched SQLite writes

## Phase 4 bot-management features

- Persistent SQLite bot profiles with stable UUIDs, display names, settings, and last-known state
- Saved profiles are restored as `Stopped`; browsers are never launched during backend startup
- Add, Start, Stop, Restart, Remove, Start All, and Stop All controls
- Each Start or Restart creates a fresh Selenium execution while keeping the same bot identity
- Removal requires confirmation, is allowed only while fully stopped, and retains all historical runs
- Thread-safe, idempotent lifecycle controls and duplicate-start protection
- Default safety policy: block starts at 90% RAM, gracefully stop at 95%, and require 1.0 GB available RAM
- Safety hysteresis prevents rapid lock/unlock behavior near resource limits
- CSV and Excel reports include stable bot ID and bot name for new runs; legacy rows remain readable

## Phase 2 playback features

- Independent Chrome WebDriver and temporary Chrome profile per worker
- Local playback fixture at `/playback-test` using `static/media/test-video.mp4`
- Readiness, playing, paused, completed, and media/browser error detection
- Live playback time, percentage, elapsed time, managed PIDs, and approximate process-tree RAM
- Start one, stop one, Start All, Stop All, and idempotent individual Stop controls
- Stable monotonic worker IDs and retained terminal worker records
- Default target and concurrency limit of one browser worker for this 8 GB laptop
- New starts blocked at the configured system RAM threshold
- Graceful managed-worker shutdown at the configured critical RAM threshold
- Sustained CPU shutdown after five consecutive 90% samples (ten seconds by default)
- Automatic cleanup when playback completes, Chrome fails, a stop is requested, or FastAPI exits
- SQLite job history plus real `psutil` CPU/RAM metrics
- Bundled Chart.js; no runtime CDN dependency

## Phase 3 analytics features

- UUID application sessions and worker run IDs
- Deduplicated worker state transitions and final statuses: `completed`, `failed`, `stopped`, and `safety_stopped`
- Execution timing, playback progress, errors, stop reasons, and managed browser process information
- CPU, RAM, available memory, active-worker count, and per-worker process-tree RAM sampled every two seconds
- Bounded memory buffer with batched SQLite writes and a guaranteed shutdown flush
- SQLite WAL mode, busy timeout, indexes, parameterized filters, and serialized writes
- Stored-data summary analytics, resource charts, filtering, sortable history, pagination, and run details
- In-memory CSV export and styled Excel export with Summary, Test Runs, Resource Samples, and Errors sheets
- Seven-day resource retention and 90-day run retention, excluding the active session

Only process trees created by this application are tracked or used for fallback termination. Unrelated Chrome instances are never selected by executable name.

## Windows prerequisites

- Python 3.11 or newer
- A current installation of Google Chrome
- Internet access may be required once by Selenium Manager to obtain a matching ChromeDriver. Later runs normally reuse its cache.

## Setup

Open PowerShell in this directory:

```powershell
py -3.11 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `.venv` already exists, activate it and run only the final install command.

## Run manually

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

Open <http://127.0.0.1:8000>. The server intentionally binds only to `127.0.0.1`.

## Recommended Windows launcher

After setup, double-click `start_app.bat` or run:

```powershell
.\start_app.bat
```

The launcher selects `.venv` when available, checks Python 3.11+, imports required packages, reports only whether `YOUTUBE_API_KEY` exists (never its value), rejects a second listener on port 8000, and runs the same `python main.py` entry point. To validate prerequisites without starting the server:

```powershell
.\start_app.bat --check
```

To save a new YouTube API key once, with hidden keyboard input:

```powershell
.\save_youtube_key.bat
```

The setup command validates the Google API-key format and stores it in the current Windows user's environment. It does not write the key to this repository, SQLite, logs, or browser JavaScript. Future application launches read that saved value automatically. A process-scoped `$env:YOUTUBE_API_KEY` value, when present, takes precedence.

Stop with `Ctrl+C` in the launcher window. This allows FastAPI's shutdown hook to stop and verify its managed Chrome/ChromeDriver process trees before exit.

Open <http://127.0.0.1:8000/analytics> for Analytics & Reports.

Two saved bot profiles are created once for a new database. They remain stopped at startup. Use **Start**, **Start All**, or **Restart** to explicitly launch Chrome when resource checks permit. A stopped profile is never restarted automatically, including after a safety shutdown.

Press `Ctrl+C` to stop the backend. Its shutdown hook requests every managed worker to stop, waits for cleanup, calls `driver.quit()`, and applies a PID/create-time-validated fallback only to its own process trees.

## Optional configuration

Set these variables in the same PowerShell window before `python main.py`:

```powershell
$env:PLAYBACK_TARGET_WORKERS = "1"
$env:PLAYBACK_MAX_WORKERS = "1"
$env:PLAYBACK_RAM_START_PERCENT = "90"
$env:PLAYBACK_RAM_SHUTDOWN_PERCENT = "95"
$env:PLAYBACK_MIN_AVAILABLE_RAM_GB = "1.0"
$env:PLAYBACK_SAFETY_HYSTERESIS_PERCENT = "5"
$env:PLAYBACK_AVAILABLE_HYSTERESIS_GB = "0.25"
$env:YOUTUBE_API_KEY = "your-server-side-api-key"
$env:YOUTUBE_API_TIMEOUT_SECONDS = "10"
$env:YOUTUBE_MAX_RESULTS = "24"
# YouTube playback has no duration limit; Stop and safety limits remain active.
$env:PLAYBACK_CPU_LIMIT_PERCENT = "90"
$env:PLAYBACK_CPU_SAMPLES = "5"
$env:PLAYBACK_HEADLESS = "true"
$env:RESOURCE_RETENTION_DAYS = "7"
$env:EXECUTION_RETENTION_DAYS = "90"
$env:RESOURCE_BUFFER_SIZE = "300"
$env:RESOURCE_BATCH_SIZE = "15"
python main.py
```

The defaults are 90% RAM start blocking, 95% graceful shutdown, 1.0 GB minimum available RAM, 90% CPU for five samples, and one concurrent browser. Use **Increase limit** for each explicit runtime increase; the backend performs the same resource safety check first.

Set `PLAYBACK_HEADLESS=false` when you want to watch the Chrome windows during manual testing. Visible windows consume more resources.

Create the YouTube key in Google Cloud, enable **YouTube Data API v3**, restrict the key to that API, then run `save_youtube_key.bat`. Never add the key to source files. Channel-name search uses `search.list`, which has a higher quota cost; handle/ID lookup and uploads-playlist retrieval are more quota-efficient.

Press **Start** on a saved bot to play the selected video without pressing **Watch Selected Video**. Bot cards show playback, progress, elapsed time, memory, and errors. Bot runs are recorded in test-run analytics with the selected video ID/title in the process details. Legacy Repeat preferences never enable looping for YouTube. **Stop**, **Stop All**, and resource shutdown close the managed browser. Increasing the browser concurrency limit does not allow concurrent YouTube sessions.

The optional **Watch Selected Video** button opens a separate visible Chrome diagnostic using the video ID saved in SQLite. It plays the selected public, embeddable video through `youtube-nocookie.com` until the video ends, the user presses **Stop**, or a resource safety limit is reached. It cannot overlap a running bot. Browser startup, player loading, playback, elapsed time, memory, and errors appear below the card. If disabled, the message directly below it reports the server-side safety reason.

The diagnostic opens the local `/youtube-player?video_id=...` page, which hosts the privacy-enhanced iframe with an explicit referrer policy and app origin. Playback and errors are monitored through the YouTube IFrame API. This supplies normal browser referrer identification rather than navigating directly to a bare embed URL (which can cause YouTube error 153).

Analytics displays the latest 100 YouTube diagnostic records across all videos and sessions, independently of worker filters. Each record includes the video ID, status, elapsed/playback time, browser memory snapshot, cleanup result, stop reason, and error. The same history is available at `GET /api/analytics/youtube-diagnostics?limit=100` (maximum 500).

## Verify Chrome cleanup

1. Close unrelated Chrome windows to make observation easier; this is optional.
2. Start the application and wait for workers to enter `Running`.
3. Note each worker's managed PID list in `GET /api/workers` or the process count in the dashboard.
4. Press **Stop All** and wait for every worker to show `Stopped`.
5. In PowerShell, check each previously reported PID:

```powershell
Get-Process -Id 1234 -ErrorAction SilentlyContinue
```

No process should be returned. You can repeat the check after stopping the backend with `Ctrl+C`. Do not use broad commands such as `Stop-Process -Name chrome`; those could terminate unrelated browser sessions.

## Tests

The unit suite uses fake WebDrivers and never launches Chrome:

```powershell
python -m pytest -v
```

It covers prior playback and analytics behavior plus profile migration/persistence, individual lifecycle operations, removal restrictions, history preservation, concurrent controls, resource enforcement, failure recovery, graceful shutdown, YouTube pagination and ownership, persisted selection state, diagnostic lifecycle/results, and Phase 6 workflow markup. External YouTube calls and WebDrivers are mocked.

The final Phase 6 validation record is in `QA_REPORT.md`.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/metrics` | CPU, RAM, managed Chrome RAM, safety state, uptime, and results |
| `GET` | `/api/dashboard/snapshot` | Consolidated dashboard polling payload |
| `GET` | `/api/performance` | Backend RSS, Chrome/ChromeDriver RAM, active browsers, and cleanup anomalies |
| `GET` | `/api/workers` | Worker status, playback progress, errors, PIDs, and memory |
| `GET` | `/api/bots` | Saved profiles, current execution state, counts, progress, and memory |
| `POST` | `/api/bots` | Create a persistent stopped bot profile |
| `PATCH` | `/api/bots/{bot_id}` | Update a profile name or settings |
| `POST` | `/api/bots/{bot_id}/start` | Start a fresh execution after safety checks |
| `POST` | `/api/bots/{bot_id}/stop` | Idempotently stop the current execution and retain the profile |
| `POST` | `/api/bots/{bot_id}/restart` | Confirm cleanup, re-check safety, and create a fresh run |
| `DELETE` | `/api/bots/{bot_id}?confirm=true` | Remove a fully stopped profile while retaining history |
| `POST` | `/api/bots/start-all` | Start eligible saved bots within safety and concurrency limits |
| `POST` | `/api/bots/stop-all` | Stop all managed browsers and retain their profiles |
| `POST` | `/api/workers/add` | Start one new worker if safe and below the limit |
| `POST` | `/api/workers/remove` | Stop the highest-ID active worker |
| `POST` | `/api/workers/start-all` | Reach the configured target using new workers |
| `POST` | `/api/workers/stop-all` | Idempotently stop every managed worker |
| `POST` | `/api/workers/{id}/stop` | Idempotently stop one managed worker |
| `POST` | `/api/workers/increase-limit` | Explicitly raise the session limit after a safety check |
| `GET` | `/api/logs` | Recent lifecycle and safety events |
| `GET` | `/api/jobs` | Recent SQLite playback results |
| `GET` | `/api/analytics/summary` | Aggregate metrics calculated from stored records |
| `GET` | `/api/analytics/sessions` | Available application sessions |
| `GET` | `/api/analytics/runs` | Filtered, sorted, paginated run history |
| `GET` | `/api/analytics/runs/{run_id}` | Detailed run and transition history |
| `GET` | `/api/analytics/resources` | Historical system and worker resource samples |
| `GET` | `/api/analytics/errors` | Filtered playback/browser errors |
| `GET` | `/api/analytics/export?format=csv` | Download filtered CSV history |
| `GET` | `/api/analytics/export?format=xlsx` | Download the four-sheet Excel report |
| `GET` | `/api/youtube/status` | Report whether the server-side API key is configured |
| `GET` | `/api/youtube/channels?q=...` | Search/resolve channels through the official API |
| `GET` | `/api/youtube/channels/{channel_id}/videos` | List public uploads and statistics |
| `GET` | `/api/youtube/selection` | Get the persisted target video |
| `POST` | `/api/youtube/selection` | Verify ownership and replace the single target selection |
| `GET` | `/api/youtube/diagnostic` | Read the isolated diagnostic state |
| `GET` | `/api/youtube/diagnostic/latest` | Read the latest persisted diagnostic result for the selected video |
| `POST` | `/api/youtube/diagnostic/start` | Manually start one single-session diagnostic |
| `POST` | `/api/youtube/diagnostic/stop` | Stop the diagnostic idempotently |
| `GET` | `/playback-test` | Local HTML5 playback fixture |
| `GET` | `/health` | Local health check |

Interactive documentation is available at <http://127.0.0.1:8000/docs>.

## Project structure

```text
app/
  api.py              FastAPI control endpoints
  analytics.py        Buffered samples and retention coordination
  analytics_api.py    Validated historical analytics endpoints
  browser_worker.py   Selenium lifecycle and process-tree ownership
  config.py           Local host, target, limits, and safety settings
  database.py         WAL-backed SQLite analytics repository
  main.py             Application factory, resource monitor, and shutdown hook
  services.py         System metrics and safety-policy coordinator
  youtube.py          Credential-safe official Data API v3 client
  youtube_api.py      Channel, video, selection, and diagnostic endpoints
  youtube_diagnostic.py  Isolated one-at-a-time diagnostic browser
  reporting.py        In-memory CSV and Excel generation
  workers.py          Thread-safe real-worker manager
  workers_model.py    Worker states and shared timestamps
static/
  css/dashboard.css
  css/analytics.css
  js/dashboard.js
  js/analytics.js
  js/chart.umd.min.js
  media/test-video.mp4
templates/
  index.html
  analytics.html
  playback_test.html
tests/
main.py               `python main.py` entry point
start_app.bat          Guarded Windows startup and prerequisite checks
save_youtube_key.bat   One-time hidden-input Windows key setup command
save_youtube_key.ps1   Validates and persists the key outside the repository
QA_REPORT.md           Phase 6 pass/fail/not-tested validation record
```

## Scope

User-started bots play the saved YouTube selection once through the embedded player. The bundled local media is retained for tests. The app binds to localhost and does not perform account actions, likes, or subscriptions.
