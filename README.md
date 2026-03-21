# SLURM Tools

A collection of utilities for monitoring and managing SLURM cluster workloads.

## slurm-monitor

A live web dashboard that shows all running SLURM jobs and their GPU utilization in real time.

### Features

- **Job list** — displays all queued/running jobs with job ID, partition, name, user, state, and elapsed time
- **GPU memory graph** — per-GPU memory usage over time for the selected job
- **GPU utilization graph** — per-GPU volatile utilization over time for the selected job
- **Auto-refresh** — configurable polling interval (default: 1 second)
- **Multi-GPU support** — each GPU is shown as a separate line on the charts

### Requirements

- Python 3.10+
- Flask (`pip install flask`)
- Access to `squeue` and `srun` commands (i.e. run on a SLURM cluster node)
- Jobs must have GPUs allocated for nvidia-smi stats to appear

### Usage

```bash
cd slurm-monitor
python3 app.py
```

Then open `http://<hostname>:5000` in a browser.

#### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `5000` | Port to listen on |
| `--host` | `0.0.0.0` | Host/IP to bind to |
| `--interval` | `1.0` | Data refresh interval in seconds |

Example with custom settings:

```bash
python3 app.py --port 8080 --interval 2
```

### How it works

1. A background thread runs `squeue` to list all cluster jobs
2. For each running job, it runs `srun --jobid <id> --overlap nvidia-smi --query-gpu=...` to collect GPU stats
3. The frontend polls `/api/jobs` and `/api/gpu/<jobid>` and renders live Chart.js graphs

### API

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web dashboard |
| `GET /api/jobs` | JSON list of all jobs from squeue |
| `GET /api/gpu/<jobid>` | JSON time-series of GPU stats for a job |
| `GET /api/config` | Current refresh interval |
