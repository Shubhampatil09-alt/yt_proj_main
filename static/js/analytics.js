let page = 1;
let pages = 0;
let sortBy = "started_at";
let sortOrder = "desc";
let resourceChart;
let workerRamChart;
const $ = id => document.getElementById(id);

async function api(path) {
  const response = await fetch(path);
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "Request failed");
  return body;
}

function filterParams(includePage = false) {
  const params = new URLSearchParams();
  const start = $("filterStart").value, end = $("filterEnd").value;
  if (start) params.set("start", new Date(start).toISOString());
  if (end) params.set("end", new Date(end).toISOString());
  if ($("filterSession").value) params.set("session_id", $("filterSession").value);
  if ($("filterWorker").value) params.set("worker_id", $("filterWorker").value);
  if ($("filterStatus").value) params.set("status", $("filterStatus").value);
  if (includePage) { params.set("page", page); params.set("page_size", 25); params.set("sort_by", sortBy); params.set("sort_order", sortOrder); }
  return params;
}

function initCharts() {
  const common = { responsive:true, maintainAspectRatio:false, animation:{duration:250}, plugins:{legend:{labels:{color:"#8d98ae"}}}, scales:{x:{grid:{display:false},ticks:{color:"#5f6a80",maxTicksLimit:7}},y:{min:0,grid:{color:"rgba(255,255,255,.045)"},ticks:{color:"#5f6a80"}}} };
  resourceChart = new Chart($("resourceChart"), {type:"line",data:{labels:[],datasets:[{label:"CPU %",data:[],borderColor:"#5598ff",pointRadius:0,tension:.3},{label:"RAM %",data:[],borderColor:"#9d78ff",pointRadius:0,tension:.3}]},options:common});
  workerRamChart = new Chart($("workerRamChart"), {type:"line",data:{labels:[],datasets:[{label:"Worker RAM MB",data:[],borderColor:"#2ee6c5",backgroundColor:"rgba(46,230,197,.08)",fill:true,pointRadius:0,tension:.3}]},options:common});
}

async function loadSessions() {
  const data = await api("/api/analytics/sessions");
  data.items.forEach(session => { const option=document.createElement("option"); option.value=session.session_id; option.textContent=`${new Date(session.started_at).toLocaleString()} · ${session.run_count} runs`; $("filterSession").append(option); });
}

async function loadAnalytics() {
  try {
    const query = filterParams().toString();
    const runQuery = filterParams(true).toString();
    const [summary,runs,resources,diagnostics] = await Promise.all([
      api(`/api/analytics/summary?${query}`), api(`/api/analytics/runs?${runQuery}`), api(`/api/analytics/resources?${query}`), api('/api/analytics/youtube-diagnostics')
    ]);
    renderSummary(summary); renderRuns(runs); renderResources(resources.items);
    $("diagnosticRows").innerHTML = diagnostics.items.map(item => `<tr><td>${escapeHtml(date(item.started_at))}</td><td>${escapeHtml(item.video_id)}</td><td>${escapeHtml(item.status)}</td><td>${Number(item.elapsed_seconds || 0).toFixed(1)}s</td><td>${escapeHtml(item.playback_status || "")} (${Number(item.playback_seconds || 0).toFixed(1)}s)</td><td>${Number(item.process?.memory_mb || 0).toFixed(1)} MB</td><td>${item.cleanup_verified == null ? "Pending" : item.cleanup_verified ? "Verified" : "Incomplete"}</td><td>${escapeHtml(item.error_message || item.stop_reason || "")}</td></tr>`).join("") || '<tr><td colspan="8">No YouTube diagnostics recorded.</td></tr>';
    $("resultContext").textContent = `${runs.total} filtered execution record${runs.total===1?"":"s"}`;
  } catch (error) { toast(error.message); }
}

function renderSummary(data) {
  const values={totalRuns:data.total_runs,successfulRuns:data.successful_runs,failedRuns:data.failed_runs,successRate:data.success_percentage,averageDuration:data.average_duration,maximumDuration:data.maximum_duration,peakCpu:data.peak_cpu,peakRam:data.peak_ram,peakWorkerRam:data.peak_worker_ram,safetyStops:data.safety_shutdowns};
  Object.entries(values).forEach(([id,value]) => $(id).textContent=value);
}

function renderResources(items) {
  const labels=items.map(x=>new Date(x.timestamp).toLocaleString());
  resourceChart.data.labels=labels; resourceChart.data.datasets[0].data=items.map(x=>x.cpu_percent); resourceChart.data.datasets[1].data=items.map(x=>x.ram_percent); resourceChart.update();
  workerRamChart.data.labels=labels; workerRamChart.data.datasets[0].data=items.map(x=>x.worker_ram_mb); workerRamChart.update();
}

function renderRuns(data) {
  pages=data.pages; page=data.page; $("runCount").textContent=`${data.total} records`; $("runsEmpty").classList.toggle("hidden",data.items.length!==0);
  $("runRows").innerHTML=data.items.map(run=>`<tr><td>${date(run.started_at)}</td><td>${run.worker_id}</td><td><span class="status ${statusClass(run.final_status)}">${escapeHtml(run.final_status||"active")}</span></td><td>${Number(run.execution_duration).toFixed(2)}s</td><td>${Number(run.playback_progress).toFixed(1)}%</td><td class="wrap">${escapeHtml(run.stop_reason||"—")}</td><td class="wrap">${escapeHtml(run.error_message||"—")}</td><td><button class="run-link" data-run="${run.run_id}">Details</button></td></tr>`).join("");
  document.querySelectorAll(".run-link").forEach(button=>button.addEventListener("click",()=>showRun(button.dataset.run)));
  $("pageLabel").textContent=`Page ${page} of ${Math.max(1,pages)}`; $("previousPage").disabled=page<=1; $("nextPage").disabled=page>=pages;
}

async function showRun(runId) {
  try { const run=await api(`/api/analytics/runs/${runId}`); $("detailTitle").textContent=`Worker ${run.worker_id} · ${run.final_status||"active"}`;
    const keys=["run_id","session_id","started_at","ended_at","execution_duration","playback_duration","playback_progress","stop_reason","error_message","browser_process_info"];
    $("runDetail").innerHTML=`<div class="detail-grid-list">${keys.map(key=>`<div class="key">${key.replaceAll("_"," ")}</div><div>${escapeHtml(typeof run[key]==="object"?JSON.stringify(run[key]):run[key]??"—")}</div>`).join("")}</div><ol class="transition-list">${run.transitions.map(item=>`<li>${date(item.timestamp)} — ${escapeHtml(item.state)}${item.details?` · ${escapeHtml(item.details)}`:""}</li>`).join("")}</ol>`;
    $("runDialog").showModal();
  } catch(error){toast(error.message);}
}

function statusClass(status){return status==="failed"?"Failed":status==="completed"?"Running":"Stopped";}
function date(value){return value?new Date(value).toLocaleString():"—";}
function escapeHtml(value){const element=document.createElement("span");element.textContent=String(value);return element.innerHTML;}
function toast(message){$("toast").textContent=message;$("toast").classList.add("show");setTimeout(()=>$("toast").classList.remove("show"),3000);}
function exportReport(format){const params=filterParams();params.set("format",format);window.location.assign(`/api/analytics/export?${params}`);}

window.addEventListener("DOMContentLoaded",async()=>{
  initCharts(); await loadSessions(); await loadAnalytics();
  $("applyFilters").addEventListener("click",()=>{page=1;loadAnalytics();});
  $("clearFilters").addEventListener("click",()=>{["filterStart","filterEnd","filterWorker"].forEach(id=>$(id).value="");["filterSession","filterStatus"].forEach(id=>$(id).value="");page=1;loadAnalytics();});
  $("previousPage").addEventListener("click",()=>{if(page>1){page--;loadAnalytics();}}); $("nextPage").addEventListener("click",()=>{if(page<pages){page++;loadAnalytics();}});
  document.querySelectorAll("th[data-sort]").forEach(th=>th.addEventListener("click",()=>{const next=th.dataset.sort;if(sortBy===next)sortOrder=sortOrder==="asc"?"desc":"asc";else{sortBy=next;sortOrder="desc";}page=1;loadAnalytics();}));
  $("exportCsv").addEventListener("click",()=>exportReport("csv")); $("exportXlsx").addEventListener("click",()=>exportReport("xlsx")); $("closeDialog").addEventListener("click",()=>$("runDialog").close());
});

