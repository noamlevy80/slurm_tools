#!/usr/bin/env python3
"""SLURM Job Monitor — live GPU stats dashboard."""

import json
import os
import subprocess
import threading
import time
from flask import Flask, jsonify, render_template_string, request

# ── Configuration ────────────────────────────────────────────────────────────
REFRESH_INTERVAL = 1  # seconds between data refreshes (configurable)

app = Flask(__name__)

# ── Shared state ─────────────────────────────────────────────────────────────
lock = threading.Lock()
jobs = []  # latest squeue snapshot
gpu_history: dict[str, list[dict]] = {}  # jobid -> [{ts, mem_used, mem_total, util}, ...]
stdout_paths: dict[str, str] = {}  # jobid -> stdout file path (cached)
MAX_HISTORY = 300  # keep ~5 min at 1 s interval


# ── Data collection ──────────────────────────────────────────────────────────
def fetch_jobs() -> list[dict]:
    """Run squeue and return a list of job dicts."""
    try:
        result = subprocess.run(
            ["squeue", "--format=%i|%P|%j|%u|%T|%M", "--noheader"],
            capture_output=True, text=True, timeout=10,
        )
        out = result.stdout.strip()
        if not out:
            return []
        parsed = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 6:
                continue
            parsed.append({
                "jobid": parts[0].strip(),
                "partition": parts[1].strip(),
                "name": parts[2].strip(),
                "user": parts[3].strip(),
                "state": parts[4].strip(),
                "time": parts[5].strip(),
            })
        return parsed
    except Exception:
        return []


def fetch_gpu_stats(jobid: str) -> list[dict] | None:
    """Run nvidia-smi via srun --overlap for a given job. Returns per-GPU stats."""
    try:
        result = subprocess.run(
            [
                "srun", "--jobid", jobid, "--overlap",
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        out = result.stdout.strip()
        if not out:
            return None
        gpus = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            gpus.append({
                "index": int(parts[0]),
                "mem_used": float(parts[1]),
                "mem_total": float(parts[2]),
                "util": float(parts[3]),
            })
        return gpus
    except Exception:
        return None


def fetch_stdout_path(jobid: str) -> str | None:
    """Get the StdOut file path for a job via scontrol."""
    try:
        result = subprocess.run(
            ["scontrol", "show", "job", jobid],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            for part in line.split():
                if part.startswith("StdOut="):
                    return part.split("=", 1)[1]
    except Exception:
        pass
    return None


def read_log_tail(path: str, max_bytes: int = 64 * 1024) -> str:
    """Read the tail of a log file."""
    try:
        size = os.path.getsize(path)
        with open(path, "r", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # skip partial line
            return f.read()
    except Exception:
        return ""


def background_loop():
    """Continuously refresh job list and GPU stats."""
    global jobs
    while True:
        new_jobs = fetch_jobs()
        running_ids = {j["jobid"] for j in new_jobs if j["state"] == "RUNNING"}

        # Collect GPU stats for all running jobs (in parallel via threads)
        gpu_results: dict[str, list[dict] | None] = {}

        def _fetch(jid):
            gpu_results[jid] = fetch_gpu_stats(jid)

        threads = [threading.Thread(target=_fetch, args=(jid,)) for jid in running_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=12)

        ts = time.time()
        with lock:
            jobs = new_jobs
            # Prune history for jobs that no longer exist
            stale = set(gpu_history.keys()) - running_ids
            for jid in stale:
                del gpu_history[jid]
            # Append new data points
            for jid, stats in gpu_results.items():
                if stats is None:
                    continue
                entry = {"ts": ts, "gpus": stats}
                gpu_history.setdefault(jid, []).append(entry)
                gpu_history[jid] = gpu_history[jid][-MAX_HISTORY:]

        time.sleep(REFRESH_INTERVAL)


# ── API routes ───────────────────────────────────────────────────────────────
@app.route("/api/jobs")
def api_jobs():
    with lock:
        return jsonify(jobs)


@app.route("/api/gpu/<jobid>")
def api_gpu(jobid):
    with lock:
        history = gpu_history.get(jobid, [])
        return jsonify(history)


@app.route("/api/log/<jobid>")
def api_log(jobid):
    # Get cached path or fetch it
    path = stdout_paths.get(jobid)
    if not path:
        path = fetch_stdout_path(jobid)
        if path:
            stdout_paths[jobid] = path
    if not path or not os.path.isfile(path):
        return jsonify({"path": None, "content": ""})
    content = read_log_tail(path)
    return jsonify({"path": path, "content": content})


@app.route("/api/config")
def api_config():
    return jsonify({"refresh_interval": REFRESH_INTERVAL})


# ── Frontend ─────────────────────────────────────────────────────────────────
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>🖥️ SLURM GPU Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e0e0e0; --text2: #888; --accent: #6c63ff; --accent2: #00c9a7;
    --running: #00c9a7; --pending: #f59e0b; --other: #888;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 1.3rem; }
  header .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--running); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
  .container { display: flex; flex: 1; overflow: hidden; }

  /* ── Left panel ── */
  .job-list { width: 520px; min-width: 400px; border-right: 1px solid var(--border); display: flex; flex-direction: column; }
  .job-list-header { display: grid; grid-template-columns: 80px 70px 1fr 90px 80px 70px; gap: 4px; padding: 10px 16px; background: var(--surface); border-bottom: 1px solid var(--border); font-size: .75rem; color: var(--text2); text-transform: uppercase; letter-spacing: .05em; }
  .job-rows { flex: 1; overflow-y: auto; }
  .job-row { display: grid; grid-template-columns: 80px 70px 1fr 90px 80px 70px; gap: 4px; padding: 10px 16px; border-bottom: 1px solid var(--border); cursor: pointer; font-size: .82rem; transition: background .15s; }
  .job-row:hover { background: rgba(108,99,255,.1); }
  .job-row.selected { background: rgba(108,99,255,.18); border-left: 3px solid var(--accent); }
  .job-row .state { font-weight: 600; }
  .job-row .state.RUNNING { color: var(--running); }
  .job-row .state.PENDING { color: var(--pending); }
  .job-row .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* ── Middle panel (graphs) ── */
  .detail { flex: 2; padding: 24px; overflow-y: auto; display: flex; flex-direction: column; gap: 20px; }
  .detail.empty { align-items: center; justify-content: center; }
  .detail.empty p { color: var(--text2); font-size: 1.1rem; }
  .chart-box { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; flex: 1; min-height: 250px; display: flex; flex-direction: column; }
  .chart-box h3 { font-size: .9rem; margin-bottom: 8px; color: var(--text2); }
  .chart-box canvas { flex: 1; }
  .job-meta { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; display: flex; gap: 24px; flex-wrap: wrap; font-size: .85rem; }
  .job-meta span { color: var(--text2); }
  .job-meta b { color: var(--text); margin-left: 4px; }

  /* ── Right panel (log output) ── */
  .log-panel { width: 33.3%; min-width: 300px; border-left: 1px solid var(--border); display: flex; flex-direction: column; background: var(--surface); }
  .log-header { padding: 10px 16px; border-bottom: 1px solid var(--border); font-size: .75rem; color: var(--text2); text-transform: uppercase; letter-spacing: .05em; display: flex; align-items: center; gap: 8px; }
  .log-header .log-path { color: var(--accent2); text-transform: none; letter-spacing: normal; font-size: .78rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .log-content { flex: 1; overflow-y: auto; padding: 12px 16px; font-family: 'Fira Code', 'Cascadia Code', 'Consolas', monospace; font-size: .75rem; line-height: 1.5; white-space: pre-wrap; word-break: break-all; color: #c8c8c8; background: #0a0c10; }
  .log-empty { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--text2); font-size: .9rem; }

  /* scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>
<header>
  <div class="dot"></div>
  <h1>SLURM GPU Monitor</h1>
  <span style="color:var(--text2); font-size:.8rem; margin-left:auto;" id="status">connecting…</span>
</header>
<div class="container">
  <div class="job-list">
    <div class="job-list-header">
      <div>Job ID</div><div>Partition</div><div>Name</div><div>User</div><div>State</div><div>Time</div>
    </div>
    <div class="job-rows" id="jobRows"></div>
  </div>
  <div class="detail empty" id="detail">
    <p>← Select a job to see GPU stats</p>
  </div>
  <div class="log-panel" id="logPanel">
    <div class="log-header">
      <span>Job Output</span>
      <span class="log-path" id="logPath"></span>
    </div>
    <div class="log-empty" id="logEmpty">Select a job to view its output</div>
    <div class="log-content" id="logContent" style="display:none;"></div>
  </div>
</div>

<script>
const COLORS = ['#6c63ff','#00c9a7','#f59e0b','#ef4444','#3b82f6','#ec4899','#14b8a6','#f97316'];
let selectedJob = null;
let memChart = null, utilChart = null;
let refreshInterval = 1000;
let pollCount = 0;
const LOG_EVERY_N = 20;

// Fetch config
fetch('/api/config').then(r=>r.json()).then(c => { refreshInterval = c.refresh_interval * 1000; startPolling(); });

function startPolling() { fetchJobs(); setInterval(fetchJobs, refreshInterval); }

async function fetchJobs() {
  try {
    const r = await fetch('/api/jobs');
    const data = await r.json();
    renderJobs(data);
    document.getElementById('status').textContent = `${data.length} jobs · refreshing every ${refreshInterval/1000}s`;
    if (selectedJob) {
      fetchGpu(selectedJob);
      if (pollCount % LOG_EVERY_N === 0) fetchLog(selectedJob);
      pollCount++;
    }
  } catch(e) {
    document.getElementById('status').textContent = 'connection lost';
  }
}

function renderJobs(data) {
  const el = document.getElementById('jobRows');
  // Preserve selection
  const sel = selectedJob;
  el.innerHTML = data.map(j => `
    <div class="job-row ${j.jobid===sel?'selected':''}" onclick="selectJob('${j.jobid}')">
      <div>${j.jobid}</div>
      <div>${j.partition}</div>
      <div class="name" title="${j.name}">${j.name}</div>
      <div>${j.user}</div>
      <div class="state ${j.state}">${j.state}</div>
      <div>${j.time}</div>
    </div>
  `).join('');
}

function selectJob(jobid) {
  selectedJob = jobid;
  pollCount = 0;  // reset so log fetches immediately on selection
  // Highlight
  document.querySelectorAll('.job-row').forEach(r => r.classList.toggle('selected', r.querySelector('div').textContent === jobid));
  fetchGpu(jobid);
  fetchLog(jobid);
}

async function fetchGpu(jobid) {
  const detail = document.getElementById('detail');
  try {
    const r = await fetch(`/api/gpu/${jobid}`);
    const history = await r.json();
    if (!history.length) {
      detail.className = 'detail empty';
      detail.innerHTML = '<p>No GPU data yet for this job…</p>';
      return;
    }
    detail.className = 'detail';
    // Determine GPU count from latest entry
    const nGpus = history[history.length-1].gpus.length;
    const latest = history[history.length-1].gpus;

    // Build meta line
    let metaHtml = `<div class="job-meta"><span>Job</span><b>${jobid}</b>`;
    for (const g of latest) {
      metaHtml += `<span>GPU ${g.index}:</span><b>${g.mem_used.toFixed(0)} / ${g.mem_total.toFixed(0)} MiB</b><span>Util</span><b>${g.util.toFixed(0)}%</b>`;
    }
    metaHtml += '</div>';

    // Ensure chart containers exist
    if (!detail.querySelector('#memCanvas')) {
      detail.innerHTML = metaHtml +
        '<div class="chart-box"><h3>GPU Memory Usage (MiB)</h3><canvas id="memCanvas"></canvas></div>' +
        '<div class="chart-box"><h3>GPU Volatile Utilization (%)</h3><canvas id="utilCanvas"></canvas></div>';
      createCharts(nGpus);
    } else {
      detail.querySelector('.job-meta').outerHTML = metaHtml;
    }
    updateCharts(history, nGpus);
  } catch(e) { /* ignore transient errors */ }
}

function createCharts(nGpus) {
  if (memChart) { memChart.destroy(); memChart = null; }
  if (utilChart) { utilChart.destroy(); utilChart = null; }
  const datasets = (metric) => Array.from({length: nGpus}, (_,i) => ({
    label: `GPU ${i}`,
    borderColor: COLORS[i % COLORS.length],
    backgroundColor: COLORS[i % COLORS.length] + '22',
    borderWidth: 2, pointRadius: 0, fill: metric === 'mem',
    tension: 0.3, data: [],
  }));

  const commonOpts = {
    responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
    scales: {
      x: { type: 'linear', display: true, title: { display: true, text: 'seconds ago', color: '#888' },
           ticks: { color: '#888', callback: v => -v.toFixed(0) }, grid: { color: '#2a2d3a' }, reverse: true },
      y: { beginAtZero: true, ticks: { color: '#888' }, grid: { color: '#2a2d3a' } }
    },
    plugins: { legend: { labels: { color: '#ccc' } } },
  };

  memChart = new Chart(document.getElementById('memCanvas'), {
    type: 'line', data: { datasets: datasets('mem') },
    options: { ...commonOpts },
  });
  utilChart = new Chart(document.getElementById('utilCanvas'), {
    type: 'line', data: { datasets: datasets('util') },
    options: { ...commonOpts, scales: { ...commonOpts.scales, y: { ...commonOpts.scales.y, max: 100 } } },
  });
}

function updateCharts(history, nGpus) {
  if (!memChart || !utilChart) return;
  // Ensure dataset count matches
  if (memChart.data.datasets.length !== nGpus) { createCharts(nGpus); }

  const now = history[history.length-1].ts;
  for (let g = 0; g < nGpus; g++) {
    const memData = [], utilData = [];
    for (const entry of history) {
      const gpu = entry.gpus.find(x => x.index === g);
      if (!gpu) continue;
      const ago = now - entry.ts;
      memData.push({ x: ago, y: gpu.mem_used });
      utilData.push({ x: ago, y: gpu.util });
    }
    memChart.data.datasets[g].data = memData;
    utilChart.data.datasets[g].data = utilData;
  }
  memChart.update('none');
  utilChart.update('none');
}

async function fetchLog(jobid) {
  const logContent = document.getElementById('logContent');
  const logEmpty = document.getElementById('logEmpty');
  const logPath = document.getElementById('logPath');
  try {
    const r = await fetch(`/api/log/${jobid}`);
    const data = await r.json();
    if (!data.path) {
      logContent.style.display = 'none';
      logEmpty.style.display = 'flex';
      logEmpty.textContent = 'No output file found for this job';
      logPath.textContent = '';
      return;
    }
    logPath.textContent = data.path;
    logContent.textContent = data.content || '(empty)';
    logContent.style.display = 'block';
    logEmpty.style.display = 'none';
    // Auto-scroll to bottom
    logContent.scrollTop = logContent.scrollHeight;
  } catch(e) { /* ignore transient errors */ }
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


# ── Start ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SLURM GPU Monitor")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--interval", type=float, default=1.0, help="Refresh interval in seconds")
    args = parser.parse_args()

    REFRESH_INTERVAL = args.interval

    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    print(f"🖥️  SLURM GPU Monitor starting on http://{args.host}:{args.port}")
    print(f"   Refresh interval: {REFRESH_INTERVAL}s")
    app.run(host=args.host, port=args.port, debug=False)
