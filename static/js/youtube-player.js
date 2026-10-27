"use strict";
let player;
const playback = {ready: false, time: 0, duration: 0, paused: true, ended: false, state: "loading player", error: null};
const statusElement = document.getElementById("playerStatus");
window.youtubeDiagnostic = {
  snapshot() {
    if (playback.ready && player) {
      playback.time = Number(player.getCurrentTime() || 0);
      playback.duration = Number(player.getDuration() || 0);
    }
    return {...playback};
  }
};
function fail(message) {
  playback.error = message;
  playback.state = "error";
  statusElement.textContent = message;
}
const iframe = document.createElement("iframe");
iframe.id = "youtubePlayer";
iframe.title = "Selected YouTube video";
iframe.referrerPolicy = "strict-origin-when-cross-origin";
iframe.allow = "autoplay; encrypted-media; fullscreen; picture-in-picture";
iframe.allowFullscreen = true;
const videoId = document.getElementById("playerContainer").dataset.videoId;
const params = new URLSearchParams({enablejsapi: "1", origin: window.location.origin, autoplay: "1", mute: "1", controls: "1", playsinline: "1"});
iframe.src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(videoId)}?${params}`;
document.getElementById("playerContainer").appendChild(iframe);
window.onYouTubeIframeAPIReady = function () {
  player = new YT.Player("youtubePlayer", {events: {
    onReady(event) {
      playback.ready = true;
      playback.state = "ready";
      statusElement.textContent = "Player ready";
      event.target.mute();
      event.target.playVideo();
    },
    onStateChange(event) {
      const labels = {"-1": "unstarted", "0": "completed", "1": "playing", "2": "paused", "3": "buffering", "5": "cued"};
      playback.state = labels[event.data] || "loading player";
      playback.paused = event.data !== 1;
      playback.ended = event.data === 0;
      statusElement.textContent = playback.state;
    },
    onError(event) {
      const detail = event.data === 153 ? "Missing HTTP Referer or player client identification." : "The selected video could not be played in the embed.";
      fail(`YouTube error ${event.data}: ${detail}`);
    },
    onAutoplayBlocked() {
      playback.state = "autoplay blocked; press Play in the player";
      statusElement.textContent = playback.state;
    }
  }});
};
const apiScript = document.createElement("script");
apiScript.src = "https://www.youtube.com/iframe_api";
apiScript.onerror = () => fail("Unable to load the YouTube player API. Check the network connection.");
document.head.appendChild(apiScript);
