const INTERVAL_MS = 2000;
const MAX_POINTS = 30;
const activeStates = new Set(["Starting", "Running", "Stopping"]);
let charts = {};
let globalBusy = false;
let refreshInFlight = false;
let busyBots = new Set();
let pendingRemoval = null;
let latestMetrics = { start_blocked: true };
let latestBots = { count: 0, max_workers: 1, total_saved: 0, bots: [] };
let latestYoutube = {
  configured: false,
  selection: null,
  diagnostic: { status: "Idle" },
  eligibility: { allowed: false, reason: "Loading safety status..." },
  latest_result: null
};
let channelCache = [];
let videoCache = [];
let selectedChannel = null;
let nextVideoPageToken = null;
let changingVideo = false;
const $ = id => document.getElementById(id);

function makeChart(canvasId, color, fill) {
  if (!window.Chart) return null;
  return new Chart($(canvasId), {
    type: "line",
    data: { labels: [], datasets: [{ data: [], borderColor: color, backgroundColor: fill, borderWidth: 2, tension: .35, pointRadius: 0, fill: true }] },
    options: { responsive: true, maintainAspectRatio: false, animation: { duration: 350 }, plugins: { legend: { display: false } },
      scales: { x: { grid: { display: false }, ticks: { color: "#5f6a80", maxTicksLimit: 6 } },
        y: { min: 0, max: 100, grid: { color: "rgba(255,255,255,.045)" }, ticks: { color: "#5f6a80", callback: v => `${v}%` } } } }
  });
}

function initCharts() {
  charts.cpu = makeChart("cpuChart", "#5598ff", "rgba(85,152,255,.10)");
  charts.ram = makeChart("ramChart", "#9d78ff", "rgba(157,120,255,.10)");
}

function addPoint(chart, label, value) {
  if (!chart) return;
  chart.data.labels.push(label); chart.data.datasets[0].data.push(value);
  if (chart.data.labels.length > MAX_POINTS) { chart.data.labels.shift(); chart.data.datasets[0].data.shift(); }
  chart.update();
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  const body = type.includes("json") ? await response.json() : {};
  if (!response.ok) {
    const detail = body.detail;
    throw new Error(typeof detail === "object" ? detail.message || "Request failed" : detail || "Request failed");
  }
  return body;
}

function escapeHtml(value) {
  const element = document.createElement("span"); element.textContent = String(value ?? ""); return element.innerHTML;
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  const hours = String(Math.floor(value / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((value % 3600) / 60)).padStart(2, "0");
  const secs = String(Math.floor(value % 60)).padStart(2, "0");
  return `${hours}:${minutes}:${secs}`;
}

function setResourceAlert(id, level, message) {
  const alert = $(id);
  alert.className = `resource-alert alert ${level}`;
  alert.textContent = message;
}

function renderResourceSafety(data) {
  const thresholds = data.thresholds || {};
  const cpuLimit = Number(thresholds.cpu_sustained ?? 90);
  const requiredSamples = Number(thresholds.cpu_samples ?? 5);
  const ramStart = Number(thresholds.ram_start_block ?? 70);
  const ramShutdown = Number(thresholds.ram_shutdown ?? 80);
  const minimumAvailable = Number(thresholds.minimum_available_ram_gb ?? 1.5);
  const cpu = Number(data.cpu_percent || 0);
  const ram = Number(data.ram_percent || 0);
  const available = Number(data.ram_available_gb || 0);
  const observedCpuSamples = Number(data.cpu_high_samples || 0);

  $("cpuPanelCurrent").textContent = `${cpu.toFixed(1)}%`;
  $("cpuThreshold").textContent = `${cpuLimit.toFixed(0)}% / ${requiredSamples} samples`;
  $("cpuSampleProgress").textContent = `${Math.min(observedCpuSamples, requiredSamples)} / ${requiredSamples}`;
  $("ramStartThreshold").textContent = `${ramStart.toFixed(0)}%`;
  $("ramShutdownThreshold").textContent = `${ramShutdown.toFixed(0)}%`;
  $("minimumAvailableRam").textContent = `${minimumAvailable.toFixed(1)} GB`;
  $("availableRamBreakdown").textContent = `${available.toFixed(2)} GB`;

  if (data.cpu_sustained) {
    $("cpuProtectionState").textContent = "Shutdown active";
    setResourceAlert("cpuSafetyAlert", "danger", `CPU stayed at or above ${cpuLimit.toFixed(0)}% for ${requiredSamples} samples. Managed workers are stopping.`);
  } else if (data.safety_source === "recovery") {
    $("cpuProtectionState").textContent = "Recovery lock";
    setResourceAlert("cpuSafetyAlert", "warning", "CPU and RAM must recover past their safety margins before worker starts unlock.");
  } else if (cpu >= cpuLimit) {
    $("cpuProtectionState").textContent = "Monitoring high load";
    setResourceAlert("cpuSafetyAlert", "warning", `CPU is ${cpu.toFixed(1)}%. Protection activates after ${requiredSamples} consecutive high samples.`);
  } else {
    $("cpuProtectionState").textContent = "Normal";
    setResourceAlert("cpuSafetyAlert", "safe", `CPU is ${cpu.toFixed(1)}%. Sustained-load protection is ready.`);
  }

  if (ram >= ramShutdown) {
    setResourceAlert("ramSafetyAlert", "danger", `RAM is ${ram.toFixed(1)}%. Managed workers are stopping at the ${ramShutdown.toFixed(0)}% shutdown threshold.`);
  } else if (data.safety_source === "recovery") {
    setResourceAlert("ramSafetyAlert", "warning", "Safety recovery lock is active. RAM and CPU must clear their hysteresis margins.");
  } else if (ram >= ramStart) {
    setResourceAlert("ramSafetyAlert", "warning", `RAM is ${ram.toFixed(1)}%. New worker starts are blocked at ${ramStart.toFixed(0)}%.`);
  } else if (available < minimumAvailable) {
    setResourceAlert("ramSafetyAlert", "warning", `Only ${available.toFixed(2)} GB RAM is available. Starts require at least ${minimumAvailable.toFixed(1)} GB.`);
  } else {
    setResourceAlert("ramSafetyAlert", "safe", `RAM is ${ram.toFixed(1)}% with ${available.toFixed(2)} GB available. Worker starts are permitted by memory limits.`);
  }
}

function renderMetrics(data) {
  latestMetrics = data;
  $("cpuMetric").textContent = data.cpu_percent.toFixed(1);
  $("ramMetric").textContent = data.ram_percent.toFixed(1);
  $("ramDetail").textContent = `${data.ram_available_gb} GB free - Chrome ${data.chrome_memory_mb} MB - Driver ${data.chromedriver_memory_mb} MB`;
  $("systemRamBreakdown").textContent = `${data.ram_used_gb} / ${data.ram_total_gb} GB (${data.ram_percent.toFixed(1)}%)`;
  $("backendRamBreakdown").textContent = `${Number(data.backend_rss_mb || 0).toFixed(1)} MB`;
  $("chromeRamBreakdown").textContent = `${Number(data.chrome_memory_mb || 0).toFixed(1)} MB`;
  $("driverRamBreakdown").textContent = `${Number(data.chromedriver_memory_mb || 0).toFixed(1)} MB`;
  $("cpuBar").style.width = `${data.cpu_percent}%`;
  $("completedMetric").textContent = data.completed_tests;
  $("uptimeMetric").textContent = formatDuration(data.uptime_seconds);
  renderResourceSafety(data);
  const label = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  addPoint(charts.cpu, label, data.cpu_percent); addPoint(charts.ram, label, data.ram_percent);
  updateControls();
}

function botCard(bot) {
  const progress = Math.max(0, Math.min(100, Number(bot.progress_percent || 0)));
  const isBusy = busyBots.has(bot.bot_id);
  const active = activeStates.has(bot.status);
  const memoryFull = Boolean(latestBots.capacity?.memory_limited);
  const canStart = !active && !latestMetrics.start_blocked && !memoryFull;
  const canStop = active && bot.status !== "Stopping";
  const canRemove = bot.status === "Stopped";
  const error = bot.last_error ? `<div class="bot-error" title="${escapeHtml(bot.last_error)}">${escapeHtml(bot.last_error)}</div>` : "";
  return `<article class="bot-card ${isBusy ? "busy" : ""}">
    <div class="bot-main">
      <div class="bot-icon" aria-hidden="true">&#9673;</div>
      <div class="bot-identity"><h3>${escapeHtml(bot.display_name)}</h3><code>${escapeHtml(bot.bot_id.slice(0, 8))}</code></div>
      <span class="status ${bot.status}">${escapeHtml(bot.status)}</span>
    </div>
    <div class="bot-stats">
      <span>Browser RAM <strong>${Number(bot.browser_memory_mb).toFixed(1)} MB</strong></span>
      <span>Playback <strong>${escapeHtml(bot.playback_state)}</strong></span>
      <span>Last duration <strong>${formatDuration(bot.last_execution_duration)}</strong></span>
    </div>
    <div class="bot-progress"><i style="width:${progress}%"></i></div>
    <div class="bot-progress-label"><span>${progress.toFixed(1)}% playback</span><span>${formatDuration(bot.elapsed_seconds)}</span></div>
    ${error}
    <p class="playback-mode">One time play · Selected YouTube video${bot.video_title ? `: ${escapeHtml(bot.video_title)}` : ""}</p>
    <div class="bot-actions">
      <button class="bot-action start" data-action="start" data-id="${bot.bot_id}" ${canStart && !isBusy ? "" : "disabled"}>Start</button>
      <button class="bot-action stop" data-action="stop" data-id="${bot.bot_id}" ${canStop && !isBusy ? "" : "disabled"}>Stop</button>
      <button class="bot-action" data-action="restart" data-id="${bot.bot_id}" ${!latestMetrics.start_blocked && !isBusy ? "" : "disabled"}>Restart</button>
      <button class="bot-action remove" data-action="remove" data-id="${bot.bot_id}" data-name="${escapeHtml(bot.display_name)}" ${canRemove && !isBusy ? "" : "disabled"}>Remove</button>
    </div>
    ${isBusy ? '<div class="loading-line"><i></i></div>' : ""}
  </article>`;
}

function renderBots(data) {
  latestBots = data;
  $("savedBots").textContent = data.total_saved;
  $("runningBots").textContent = data.running_bots;
  $("stoppedBots").textContent = data.stopped_bots;
  $("failedBots").textContent = data.failed_bots;
  $("workerPill").textContent = `${data.count} / ${data.max_workers} active`;
  const capacity = data.capacity || { more_bots: 0, memory_limited: false, message: "Checking memory capacity..." };
  $("botCapacity").textContent = `${capacity.message}${capacity.estimate_mb ? ` (about ${Math.round(capacity.estimate_mb)} MB per bot)` : ""}`;
  $("botCapacity").className = `alert ${capacity.memory_limited ? "danger" : capacity.more_bots > 0 ? "safe" : "warning"}`;
  $("workerEmpty").classList.toggle("hidden", data.bots.length !== 0);
  $("botList").innerHTML = data.bots.map(botCard).join("");
  document.querySelectorAll("[data-bot-mode]").forEach(input => input.addEventListener("change", async () => {
    const botId = input.dataset.botMode;
    busyBots.add(botId);
    try {
      await api(`/api/bots/${botId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings: { playback_mode: input.value } }) });
      showToast("Playback mode saved");
    } catch (error) { showToast(error.message, true); }
    finally { busyBots.delete(botId); await refresh(); }
  }));
  document.querySelectorAll(".bot-action").forEach(button => button.addEventListener("click", () => {
    if (button.dataset.action === "remove") return askRemove(button.dataset.id, button.dataset.name);
    botAction(button.dataset.id, button.dataset.action);
  }));
  updateControls();
}

function renderLogs(data) {
  $("activityLogs").innerHTML = data.logs.length ? data.logs.map(log => `<div class="log"><time>${new Date(log.time).toLocaleTimeString()}</time><span class="log-level ${log.level}">${escapeHtml(log.level)}</span><span>${escapeHtml(log.message)}</span></div>`).join("") : '<div class="empty-state">No activity recorded.</div>';
}

function compactNumber(value) {
  return value === null || value === undefined ? "Unavailable" : new Intl.NumberFormat(undefined, { notation: "compact" }).format(value);
}

function renderYoutube(data) {
  latestYoutube = data;
  $("youtubeConfig").textContent = data.configured ? "API configured" : "YOUTUBE_API_KEY required";
  $("youtubeConfig").className = `integration-badge ${data.configured ? "ready" : "missing"}`;
  renderSelection(data.selection);
  const diagnostic = data.diagnostic || { status: "Idle" };
  const memory = Number(diagnostic.process?.memory_mb || 0).toFixed(1);
  $("diagnosticState").textContent = diagnostic.status || "Idle";
  $("diagnosticElapsed").textContent = formatDuration(diagnostic.elapsed_seconds || 0);
  $("diagnosticMemory").textContent = `${memory} MB`;
  $("diagnosticBrowser").textContent = diagnostic.browser_status || (diagnostic.process?.pids?.length ? "Open" : "Closed");
  $("diagnosticPlayback").textContent = diagnostic.playback_status || `${Number(diagnostic.playback_seconds || 0).toFixed(1)}s`;
  const error = diagnostic.last_error || data.latest_result?.error_message;
  $("diagnosticError").classList.toggle("hidden", !error);
  $("diagnosticError").textContent = error || "";
  renderDiagnosticResults(data.latest_result, diagnostic);
  renderWorkflow(data.selection, diagnostic, data.latest_result);
  updateControls();
}

function renderSelection(selection) {
  if (!selection) {
    $("selectedVideoDetails").innerHTML = '<div class="empty-state compact">No target video selected.</div>';
    $("selectionToolbar").classList.add("hidden");
    $("discoveryArea").classList.remove("collapsed");
    return;
  }
  const stats = selection.statistics || {};
  $("selectedVideoDetails").innerHTML = `<div class="selection-card">
    <img src="${escapeHtml(selection.thumbnail_url || "")}" alt="Thumbnail for ${escapeHtml(selection.video_title)}">
    <div><div class="selection-confirmation">&#10003; Video Selected</div>
      <h3>${escapeHtml(selection.video_title)}</h3>
      <p>${escapeHtml(selection.channel_title)} &middot; ${formatDuration(selection.duration_seconds)}</p>
      <p>${escapeHtml(selection.channel_id)} &middot; ${escapeHtml(selection.video_id)}</p>
      <a class="video-url" href="${escapeHtml(selection.video_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(selection.video_url)}</a>
      <div class="selection-stats"><span>${compactNumber(stats.viewCount)} views</span><span>${compactNumber(stats.likeCount)} likes</span><span>${compactNumber(stats.commentCount)} comments</span></div>
    </div></div>`;
  $("selectionToolbar").classList.remove("hidden");
  if (!changingVideo) $("discoveryArea").classList.add("collapsed");
}

function renderWorkflow(selection, diagnostic, latestResult) {
  const active = ["Starting", "Running", "Stopping"].includes(diagnostic.status);
  const terminal = ["Completed", "Failed", "Stopped", "Safety Stopped"].includes(diagnostic.status);
  let current = "search";
  if (selection) current = active ? "diagnostic" : (terminal || latestResult ? "results" : "diagnostic");
  if (changingVideo || (!selection && selectedChannel)) current = "select";
  const order = ["search", "select", "diagnostic", "results"];
  const currentIndex = order.indexOf(current);
  document.querySelectorAll("#workflowSteps [data-step]").forEach((step, index) => {
    step.classList.toggle("active", step.dataset.step === current);
    step.classList.toggle("complete", index < currentIndex);
  });
}

function renderDiagnosticResults(result, diagnostic) {
  if (["Starting", "Running", "Stopping"].includes(diagnostic.status)) {
    $("diagnosticResults").classList.add("hidden");
    $("diagnosticResults").innerHTML = "";
    return;
  }
  const terminal = ["Completed", "Failed", "Stopped", "Safety Stopped"].includes(diagnostic.status);
  const record = terminal && diagnostic.diagnostic_id ? { ...result, ...diagnostic } : result;
  if (!record) {
    $("diagnosticResults").classList.add("hidden");
    $("diagnosticResults").innerHTML = "";
    return;
  }
  const process = record.process || {};
  const cleanup = record.cleanup_verified === true ? "Verified" : (record.cleanup_verified === false ? "Needs attention" : "Pending");
  $("diagnosticResults").innerHTML = `<h3>Diagnostic Results &middot; ${escapeHtml(record.status || "Unknown")}</h3>
    <div class="result-grid">
      <div>Elapsed<strong>${formatDuration(record.elapsed_seconds || 0)}</strong></div>
      <div>Playback<strong>${Number(record.playback_seconds || 0).toFixed(1)} seconds</strong></div>
      <div>Process RAM<strong>${Number(process.memory_mb || 0).toFixed(1)} MB</strong></div>
      <div>Cleanup<strong>${cleanup}</strong></div>
    </div>
    ${record.stop_reason ? `<p class="result-note">Stop reason: ${escapeHtml(record.stop_reason)}</p>` : ""}
    <a class="results-link" href="/analytics">Open Analytics &amp; Reports &rarr;</a>`;
  $("diagnosticResults").classList.remove("hidden");
}

function renderChannels(items) {
  channelCache = items;
  $("channelResults").innerHTML = items.length ? items.map(channel => `<div class="channel-result ${selectedChannel?.channel_id === channel.channel_id ? "selected" : ""}" data-channel="${channel.channel_id}">
    <img src="${escapeHtml(channel.thumbnail_url || "")}" alt=""><div><strong>${escapeHtml(channel.title)}</strong><small>${escapeHtml(channel.channel_id)} - ${compactNumber(channel.subscriber_count)} subscribers</small></div><button type="button">Videos</button></div>`).join("") : '<div class="empty-state compact">No matching channels found.</div>';
  document.querySelectorAll(".channel-result").forEach(item => item.addEventListener("click", () => loadVideos(item.dataset.channel)));
}

function renderVideos(items, append = false) {
  videoCache = append ? [...videoCache, ...items] : items;
  const selectedId = latestYoutube.selection?.video_id;
  $("videoResults").innerHTML = videoCache.length ? videoCache.map(video => `<div class="video-result ${selectedId === video.video_id ? "selected" : ""}">
    <img src="${escapeHtml(video.thumbnail_url || "")}" alt=""><div><strong title="${escapeHtml(video.title)}">${escapeHtml(video.title)}</strong><div class="video-meta"><span>${formatDuration(video.duration_seconds)}</span><span>${new Date(video.published_at).toLocaleDateString()}</span><span>${compactNumber(video.statistics?.viewCount)} views</span></div></div><button type="button" data-video="${video.video_id}">Select</button></div>`).join("") : '<div class="empty-state compact">No available public uploads found.</div>';
  document.querySelectorAll("[data-video]").forEach(button => button.addEventListener("click", () => selectVideo(button.dataset.video)));
  $("loadMoreVideos").classList.toggle("hidden", !nextVideoPageToken);
}

async function searchChannels(event) {
  event.preventDefault();
  const query = $("channelQuery").value.trim();
  if (!query) return;
  $("searchChannels").disabled = true;
  $("channelResults").innerHTML = '<div class="empty-state compact">Searching...</div>';
  try { renderChannels((await api(`/api/youtube/channels?q=${encodeURIComponent(query)}`)).items); }
  catch (error) { $("channelResults").innerHTML = `<div class="alert danger">${escapeHtml(error.message)}</div>`; }
  finally { $("searchChannels").disabled = false; }
}

async function loadVideos(channelId, pageToken = null, append = false) {
  if (!append) {
    selectedChannel = channelCache.find(item => item.channel_id === channelId);
    nextVideoPageToken = null;
    videoCache = [];
    renderChannels(channelCache);
    $("videoHeading").textContent = selectedChannel ? `${selectedChannel.title} videos` : "Channel videos";
    $("videoResults").innerHTML = '<div class="empty-state compact">Loading uploads...</div>';
  }
  $("loadMoreVideos").disabled = true;
  try {
    const query = pageToken ? `?page_token=${encodeURIComponent(pageToken)}` : "";
    const response = await api(`/api/youtube/channels/${channelId}/videos${query}`);
    nextVideoPageToken = response.next_page_token || null;
    renderVideos(response.items || [], append);
    renderWorkflow(latestYoutube.selection, latestYoutube.diagnostic || {}, latestYoutube.latest_result);
  } catch (error) {
    if (!append) $("videoResults").innerHTML = `<div class="alert danger">${escapeHtml(error.message)}</div>`;
    else showToast(error.message, true);
  } finally {
    $("loadMoreVideos").disabled = false;
  }
}

async function selectVideo(videoId) {
  if (!selectedChannel) return;
  try {
    const response = await api("/api/youtube/selection", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ channel_id: selectedChannel.channel_id, video_id: videoId }) });
    latestYoutube.selection = response.selection;
    latestYoutube.latest_result = null;
    changingVideo = false;
    renderSelection(response.selection);
    renderVideos(videoCache);
    renderWorkflow(response.selection, latestYoutube.diagnostic || {}, null);
    showToast("Video selected, saved, and ownership verified");
  } catch (error) { showToast(error.message, true); }
}

function changeVideo() {
  changingVideo = true;
  $("discoveryArea").classList.remove("collapsed");
  renderWorkflow(latestYoutube.selection, latestYoutube.diagnostic || {}, latestYoutube.latest_result);
  $("channelQuery").focus();
  $("discoveryArea").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function updateControls() {
  const blocked = latestMetrics.start_blocked || globalBusy || Boolean(latestBots.capacity?.memory_limited);
  $("addBot").disabled = globalBusy;
  $("startAll").disabled = blocked || latestBots.count >= latestBots.max_workers || latestBots.total_saved === 0;
  $("stopAll").disabled = globalBusy || latestBots.count === 0;
  $("increaseLimit").disabled = blocked;
  const diagnosticActive = ["Starting", "Running", "Stopping"].includes(latestYoutube.diagnostic?.status);
  // Leave Search clickable when configuration is missing so the server can
  // return its safe, actionable setup message. Credentials stay backend-only.
  $("searchChannels").disabled = globalBusy;
  const eligibility = latestYoutube.eligibility || { allowed: false, reason: "Waiting for a safety check." };
  const diagnosticBlocked = globalBusy || diagnosticActive || !eligibility.allowed;
  const diagnosticReason = globalBusy
    ? "An operation is already in progress."
    : diagnosticActive
      ? `A diagnostic is already ${latestYoutube.diagnostic.status.toLowerCase()}.`
      : eligibility.reason;
  const startButton = $("startDiagnostic");
  const stopButton = $("stopDiagnostic");
  startButton.disabled = diagnosticBlocked;
  startButton.textContent = diagnosticActive
    ? `Diagnostic ${latestYoutube.diagnostic.status}`
    : "Watch Selected Video";
  startButton.title = !diagnosticBlocked ? "Play the selected video once" : diagnosticReason;
  $("diagnosticReason").textContent = !diagnosticBlocked
    ? "Ready. Plays once until the video ends, you press Stop, or a safety limit is reached."
    : diagnosticReason;
  $("diagnosticReason").classList.toggle("ready", !diagnosticBlocked);
  stopButton.disabled = globalBusy || !diagnosticActive || latestYoutube.diagnostic?.status === "Stopping";
  stopButton.title = diagnosticActive ? "Stop the active diagnostic" : "No diagnostic is currently active.";
}

function showToast(message, error = false) {
  $("toast").textContent = message; $("toast").className = `toast show ${error ? "toast-error" : ""}`;
  setTimeout(() => $("toast").classList.remove("show"), 3200);
}

async function refresh() {
  if (refreshInFlight || document.hidden) return;
  refreshInFlight = true;
  try {
    const snapshot = await api("/api/dashboard/snapshot");
    renderMetrics(snapshot.metrics); renderBots(snapshot.bots); renderLogs({ logs: snapshot.logs }); renderYoutube(snapshot.youtube);
    $("connectionStatus").textContent = "Live - 2s refresh"; $("connectionStatus").className = "connection online";
  } catch (_) {
    $("connectionStatus").textContent = "Connection lost"; $("connectionStatus").className = "connection offline";
  } finally {
    refreshInFlight = false;
  }
}

async function botAction(botId, action) {
  if (busyBots.has(botId)) return;
  busyBots.add(botId); renderBots(latestBots);
  try {
    const state = await api(`/api/bots/${botId}/${action}`, { method: "POST" });
    renderBots(state); showToast(`${action[0].toUpperCase() + action.slice(1)} requested`);
  } catch (error) { showToast(error.message, true); }
  finally { busyBots.delete(botId); await refresh(); }
}

async function groupAction(path, message) {
  if (globalBusy) return;
  globalBusy = true; updateControls();
  try { renderBots(await api(path, { method: "POST" })); showToast(message); }
  catch (error) { showToast(error.message, true); }
  finally { globalBusy = false; await refresh(); }
}

async function diagnosticAction(path, message) {
  if (globalBusy) return;
  globalBusy = true; updateControls();
  try {
    latestYoutube.diagnostic = await api(path, { method: "POST" });
    renderYoutube(latestYoutube); showToast(message);
  } catch (error) { showToast(error.message, true); }
  finally { globalBusy = false; await refresh(); }
}

function askRemove(botId, name) {
  pendingRemoval = botId;
  $("removeMessage").textContent = `${name} will be removed. Historical execution records will be retained.`;
  $("removeDialog").showModal();
}

async function removeConfirmed(event) {
  event.preventDefault();
  if (!pendingRemoval) return;
  const botId = pendingRemoval; $("removeDialog").close(); pendingRemoval = null;
  busyBots.add(botId);
  try { renderBots(await api(`/api/bots/${botId}?confirm=true`, { method: "DELETE" })); showToast("Bot profile removed; history retained"); }
  catch (error) { showToast(error.message, true); }
  finally { busyBots.delete(botId); await refresh(); }
}

async function createBot(event) {
  event.preventDefault();
  const name = $("botName").value.trim();
  try {
    await api("/api/bots", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ display_name: name || null, settings: {} }) });
    $("botDialog").close(); $("botName").value = ""; showToast("Saved bot created"); await refresh();
  } catch (error) { showToast(error.message, true); }
}

window.addEventListener("DOMContentLoaded", () => {
  initCharts();
  $("addBot").addEventListener("click", () => $("botDialog").showModal());
  $("saveBot").addEventListener("click", createBot);
  $("confirmRemove").addEventListener("click", removeConfirmed);
  $("startAll").addEventListener("click", () => groupAction("/api/bots/start-all", "Eligible bots started"));
  $("stopAll").addEventListener("click", () => groupAction("/api/bots/stop-all", "All managed browsers stopping"));
  $("increaseLimit").addEventListener("click", () => groupAction("/api/workers/increase-limit", "Concurrency limit increased"));
  $("refreshButton").addEventListener("click", refresh);
  $("youtubeSearchForm").addEventListener("submit", searchChannels);
  $("loadMoreVideos").addEventListener("click", () => {
    if (selectedChannel && nextVideoPageToken) {
      loadVideos(selectedChannel.channel_id, nextVideoPageToken, true);
    }
  });
  $("changeVideo").addEventListener("click", changeVideo);
  $("startDiagnostic").addEventListener("click", () => diagnosticAction("/api/youtube/diagnostic/start", "Manual YouTube diagnostic started"));
  $("stopDiagnostic").addEventListener("click", () => diagnosticAction("/api/youtube/diagnostic/stop", "YouTube diagnostic stopping"));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  refresh(); setInterval(refresh, INTERVAL_MS);
});
