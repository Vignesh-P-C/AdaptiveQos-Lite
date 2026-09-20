"""
dashboard/app.py -- Week 9 minimal monitoring dashboard.

A single-page Flask app that reads the project's existing Log A/B/C CSVs
(data/raw/) and serves them as JSON for a Chart.js frontend. No database,
no login, no WebSockets -- the frontend just polls these endpoints every
few seconds. Read-only: this never writes to the controller or the logs.

Run (while main_app.py / static_ecmp_only.py is running against a live
topology, so the logs are actively being written to):
    cd ~/AdaptiveQoS-Lite
    python3 dashboard/app.py
Then open http://127.0.0.1:5050/
"""

import csv
import glob
import os
import time

from flask import Flask, jsonify, render_template

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RAW = os.path.join(ROOT, "data", "raw")

# Mirrors controller/ecmp_fallback.py's NODE_TO_DPID and main_app.py's
# CANDIDATE_PATHS -- duplicated here (not imported) so the dashboard has
# zero dependency on os_ken/ryu being importable, since it may run in a
# different Python environment than the controller itself.
NODE_TO_DPID = {"s1": 1, "s2": 2, "s3": 3, "s4": 4}
CANDIDATE_PATHS = ["s1-s2-s4", "s1-s3-s4"]

STALE_AFTER_SEC = 10  # no fresh rows within this window -> considered disconnected

app = Flask(__name__)


RECENT_FILE_WINDOW_SEC = 300  # bounds how many old CSVs a single request re-reads


def _recent_flow_metrics_files():
    """Telemetry (main_app.py) and the h1-h2-realtime ping sampler
    (generate_traffic.py) each open their OWN FlowMetricsLogger at
    slightly different times, so they write to two DIFFERENT CSV files
    for the same run. Picking just "the most recently modified file"
    can miss whichever of the two wasn't touched in the last instant --
    scan every file modified recently instead of assuming there's one
    canonical "current" file."""
    now = time.time()
    files = glob.glob(os.path.join(RAW, "flow_metrics_*.csv"))
    return [f for f in files if now - os.path.getmtime(f) < RECENT_FILE_WINDOW_SEC]


def _read_csv(path, limit_last=None):
    if not path or not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit_last:] if limit_last else rows


def _read_csvs(paths):
    rows = []
    for p in paths:
        rows.extend(_read_csv(p))
    rows.sort(key=lambda r: _to_float(r.get("timestamp")))
    return rows


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_arm_estimates(s):
    """'s1-s2-s4=-1.94;s1-s3-s4=-2.14' -> {'s1-s2-s4': -1.94, 's1-s3-s4': -2.14}"""
    out = {}
    if not s:
        return out
    for part in s.split(";"):
        if "=" in part:
            path, val = part.rsplit("=", 1)
            out[path.strip()] = _to_float(val)
    return out


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    files = _recent_flow_metrics_files()
    now = time.time()

    controller_connected = False
    active_flows = 0
    scenario = None
    method = None
    if files:
        controller_connected = any((now - os.path.getmtime(f)) < STALE_AFTER_SEC for f in files)
        rows = _read_csvs(files)
        if rows:
            recent = [r for r in rows if now - _to_float(r["timestamp"]) < STALE_AFTER_SEC]
            # exclude link:<dpid>:<port> rows -- those are per-switch-port
            # telemetry, logged every second regardless of real traffic, not
            # actual user flows. Counting them made "active flows" wildly
            # overstate what's really passing through the network.
            real_flows = {r["flow_id"] for r in recent
                          if not r.get("flow_id", "").startswith("link:")}
            active_flows = len(real_flows)
            scenario = rows[-1].get("scenario")
            method = rows[-1].get("method")

    agent_rows = _read_csv(os.path.join(RAW, "agent_internal_log.csv"))
    seconds_since_last_decision = None
    current_path = None
    if agent_rows:
        last = agent_rows[-1]
        seconds_since_last_decision = round(now - _to_float(last["timestamp"]), 1)
        current_path = last.get("chosen_path")

    return jsonify({
        "controller_connected": controller_connected,
        "seconds_since_last_decision": seconds_since_last_decision,
        "active_flows": active_flows,
        "current_path": current_path,
        "scenario": scenario,
        "method": method,
    })


@app.route("/api/current_flow")
def api_current_flow():
    rows = _read_csvs(_recent_flow_metrics_files())
    realtime_rows = [r for r in rows if r.get("flow_id") == "h1-h2-realtime"]
    if not realtime_rows:
        return jsonify({})
    last = realtime_rows[-1]
    return jsonify({
        "flow_id": last.get("flow_id"),
        "flow_type": last.get("flow_type"),
        "latency_ms": _to_float(last.get("latency_ms")),
        "jitter_ms": _to_float(last.get("jitter_ms")),
        "packet_loss_pct": _to_float(last.get("packet_loss_pct")),
    })


@app.route("/api/paths")
def api_paths():
    """Per-candidate-path Q-value/visit-count (real, from Log B) plus a
    latency/jitter reading via the middle switch's telemetry (the same
    echo-RTT control-channel proxy documented as a known limitation in
    the project report -- shown here for a live demo view, not as a
    validated per-path data-plane measurement)."""
    agent_rows = _read_csv(os.path.join(RAW, "agent_internal_log.csv"))
    fm_rows = _read_csvs(_recent_flow_metrics_files())

    latest_estimates = _parse_arm_estimates(agent_rows[-1]["arm_estimates"]) if agent_rows else {}

    out = []
    for path in CANDIDATE_PATHS:
        visits = sum(1 for r in agent_rows if r.get("chosen_path") == path)
        parts = path.split("-")
        mid_dpid = NODE_TO_DPID.get(parts[1]) if len(parts) == 3 else None
        link_rows = [r for r in fm_rows if r.get("flow_id", "").startswith(f"link:{mid_dpid}:")] \
            if mid_dpid else []
        latency_ms = jitter_ms = None
        if link_rows:
            last_link = link_rows[-1]
            latency_ms = _to_float(last_link.get("latency_ms"))
            jitter_ms = _to_float(last_link.get("jitter_ms"))
        out.append({
            "path": path,
            "q_value": latest_estimates.get(path),
            "visits": visits,
            "latency_ms": latency_ms,
            "jitter_ms": jitter_ms,
        })
    return jsonify(out)


@app.route("/api/decisions")
def api_decisions():
    rows = _read_csv(os.path.join(RAW, "agent_internal_log.csv"), limit_last=15)
    out = [{
        "timestamp": _to_float(r["timestamp"]),
        "flow_id": r.get("flow_id"),
        "chosen_path": r.get("chosen_path"),
        "reward": _to_float(r.get("reward")),
    } for r in reversed(rows)]
    return jsonify(out)


@app.route("/api/timeseries")
def api_timeseries():
    rows = _read_csvs(_recent_flow_metrics_files())
    realtime_rows = [r for r in rows if r.get("flow_id") == "h1-h2-realtime"][-60:]
    return jsonify({
        "timestamps": [_to_float(r["timestamp"]) for r in realtime_rows],
        "latency_ms": [_to_float(r["latency_ms"]) for r in realtime_rows],
        "jitter_ms": [_to_float(r["jitter_ms"]) for r in realtime_rows],
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
