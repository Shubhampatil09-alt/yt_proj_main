# Manual selected-video diagnostic validation

Date: 2026-10-17

- Full regression suite after the error 153 correction: **55 passed**, one third-party Starlette/httpx deprecation warning, 19.72 seconds.
- `node tests/test_youtube_player.cjs`: iframe domain/video/origin/referrer configuration, playback state propagation, no repeat after completion, and error 153 reporting passed.
- The diagnostic opens `/youtube-player?video_id=...`, which embeds YouTube with `strict-origin-when-cross-origin` and uses the IFrame API for monitoring. It no longer opens the embed directly or inspects cross-origin video elements.
- JavaScript syntax checks: dashboard.js and analytics.js passed `node --check`.
- Tests cover persisted video selection, exact youtube-nocookie embed URL, manual Start/Stop, single-session/concurrency enforcement, RAM and sustained CPU shutdown, cleanup, historical analytics, visible Chrome configuration, and existing local bot modes.
- Updated the Phase 6 RAM-block test to use the configured threshold rather than the obsolete hardcoded 75% value.
- External YouTube API calls and browser sessions are mocked in tests. Live Chrome/YouTube playback was not exercised.
- The diagnostic has no playback duration limit. It runs until video completion or manual/safety stop and never automatically repeats or restarts.
