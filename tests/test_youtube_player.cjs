const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const container = {dataset: {videoId: 'AbCdEf123_-'}, appendChild(node) { this.iframe = node; }};
const status = {};
const head = {appendChild(node) { this.script = node; }};
let events;
let played = 0;
let muted = 0;
const player = {getCurrentTime: () => 4.5, getDuration: () => 49, mute() { muted++; }, playVideo() { played++; }};
const window = {location: {origin: 'http://127.0.0.1:8000'}};
const context = {
  window, URLSearchParams,
  document: {head, getElementById: id => id === 'playerContainer' ? container : status, createElement: () => ({})},
  YT: {Player: function(id, config) { assert.equal(id, 'youtubePlayer'); events = config.events; return player; }}
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/js/youtube-player.js'), 'utf8'), context);
const url = new URL(container.iframe.src);
assert.equal(url.origin, 'https://www.youtube-nocookie.com');
assert.equal(url.pathname, '/embed/AbCdEf123_-');
assert.equal(url.searchParams.get('origin'), window.location.origin);
assert.equal(url.searchParams.get('enablejsapi'), '1');
assert.equal(url.searchParams.has('loop'), false);
assert.equal(container.iframe.referrerPolicy, 'strict-origin-when-cross-origin');
window.onYouTubeIframeAPIReady();
events.onReady({target: player});
assert.equal(played, 1);
assert.equal(muted, 1);
events.onStateChange({data: 1});
assert.equal(window.youtubeDiagnostic.snapshot().state, 'playing');
assert.equal(window.youtubeDiagnostic.snapshot().time, 4.5);
events.onStateChange({data: 3});
assert.equal(window.youtubeDiagnostic.snapshot().state, 'buffering');
events.onStateChange({data: 0});
assert.equal(window.youtubeDiagnostic.snapshot().ended, true);
assert.equal(played, 1); // Completion must never restart YouTube playback.
events.onError({data: 153});
assert.match(window.youtubeDiagnostic.snapshot().error, /153.*Referer/);
console.log('YouTube iframe setup, playback states, no-repeat, and error propagation passed.');
