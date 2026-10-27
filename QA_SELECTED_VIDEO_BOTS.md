# Selected-video bots

Date: 2026-10-17

The production worker factory now creates a YouTubeWorker from the SQLite selection rather than launching the local MP4. It shares the iframe loading/state helpers with the manual diagnostic and inherits worker history, managed-process cleanup, Stop, and safety lifecycle behavior.

- Full regression suite: **62 passed**, one Starlette/httpx dependency deprecation warning, 23.03 seconds.
- JavaScript syntax and iframe behavior checks passed.
- Added integration coverage for missing selection, exact selected-video URL, completion and analytics history, legacy Repeat suppression, manual Stop, one-session enforcement after concurrency increases, diagnostic/bot exclusion in both directions, Start One Bot behavior, resource shutdown, and player errors.
- Updated dashboard asset version so refreshed clients receive the new bot controls.
- Browser and external YouTube API interactions are mocked; live playback has not been verified.

Restart the server and refresh the dashboard. Select/save a video, then press Start on a bot. The video plays once with no duration cap, automatic repeat, or automatic restart. Stop, Stop All, resource protections, and the completion event close the managed browser. The optional Watch Selected Video diagnostic remains available when no bot session is active.
