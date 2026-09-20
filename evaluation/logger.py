"""
evaluation/logger.py — Log A / B / C writers

Schemas match Build Plan §2 exactly. Kept dependency-free (stdlib
csv only) so it can be imported from inside the Ryu app (telemetry.py,
agent.py) without dragging pandas into the controller process — pandas
only shows up later in analyze_results.py, run offline.

Each logger opens its file in append mode and flushes every row, since
a controller process can be killed ungracefully (Ctrl-C on ryu-manager,
a Mininet crash) and partial-but-flushed data beats a corrupted buffer.
"""

import csv
import os
import time


DATA_RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw")


def _ensure_dir():
    os.makedirs(DATA_RAW_DIR, exist_ok=True)


class FlowMetricsLogger:
    """Log A — data/raw/flow_metrics_<scenario>_<method>_<timestamp>.csv
    One row per measurement interval, per active flow/link."""

    COLUMNS = [
        "timestamp", "scenario", "method", "flow_id", "flow_type",
        "path_chosen", "latency_ms", "jitter_ms", "packet_loss_pct",
        "throughput_mbps",
    ]

    def __init__(self, method, scenario, run_timestamp=None):
        _ensure_dir()
        self.method = method
        self.scenario = scenario
        ts = run_timestamp or time.strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(
            DATA_RAW_DIR, f"flow_metrics_{scenario}_{method}_{ts}.csv"
        )
        self._init_file()

    def _init_file(self):
        new_file = not os.path.exists(self.path)
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.COLUMNS)
        if new_file:
            self._writer.writeheader()
            self._fh.flush()

    def write_row(self, flow_id, flow_type, path_chosen, latency_ms,
                  jitter_ms, packet_loss_pct, throughput_mbps):
        self._writer.writerow({
            "timestamp": time.time(),
            "scenario": self.scenario,
            "method": self.method,
            "flow_id": flow_id,
            "flow_type": flow_type,
            "path_chosen": path_chosen,
            "latency_ms": latency_ms,
            "jitter_ms": jitter_ms,
            "packet_loss_pct": packet_loss_pct,
            "throughput_mbps": throughput_mbps,
        })
        self._fh.flush()

    def close(self):
        self._fh.close()


class AgentInternalLogger:
    """Log B — data/raw/agent_internal_log.csv
    One row per routing decision the agent makes. Stage 4 / week 5-6
    scope — included now so agent.py has something to write to as soon
    as it exists, without touching this file again."""

    COLUMNS = [
        "timestamp", "scenario", "flow_id", "candidate_paths", "chosen_path",
        "reward", "arm_estimates",
    ]

    def __init__(self, scenario="unlabeled"):
        _ensure_dir()
        self.scenario = scenario
        self.path = os.path.join(DATA_RAW_DIR, "agent_internal_log.csv")
        new_file = not os.path.exists(self.path)
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.COLUMNS)
        if new_file:
            self._writer.writeheader()
            self._fh.flush()

    def write_row(self, flow_id, candidate_paths, chosen_path, reward, arm_estimates):
        self._writer.writerow({
            "timestamp": time.time(),
            "scenario": self.scenario,
            "flow_id": flow_id,
            "candidate_paths": candidate_paths,
            "chosen_path": chosen_path,
            "reward": reward,
            "arm_estimates": arm_estimates,
        })
        self._fh.flush()

    def close(self):
        self._fh.close()


class SetupCostLogger:
    """Log C — data/raw/setup_cost_log.csv
    One row per method, per cold-start timing run. Manual: call
    start()/finish() around bringing each method online. Time each
    method at least 3x and average, per the build plan."""

    COLUMNS = [
        "method", "setup_start", "working_state_reached",
        "wall_clock_seconds", "peak_ram_mb", "gpu_used",
    ]

    def __init__(self):
        _ensure_dir()
        self.path = os.path.join(DATA_RAW_DIR, "setup_cost_log.csv")
        new_file = not os.path.exists(self.path)
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.COLUMNS)
        if new_file:
            self._writer.writeheader()
            self._fh.flush()
        self._starts = {}

    def start(self, method):
        self._starts[method] = time.time()

    def finish(self, method, peak_ram_mb=None, gpu_used="no"):
        start = self._starts.pop(method, None)
        if start is None:
            raise ValueError(f"start('{method}') was never called")
        end = time.time()
        self._writer.writerow({
            "method": method,
            "setup_start": start,
            "working_state_reached": end,
            "wall_clock_seconds": round(end - start, 3),
            "peak_ram_mb": peak_ram_mb if peak_ram_mb is not None else "",
            "gpu_used": gpu_used,
        })
        self._fh.flush()

    def close(self):
        self._fh.close()
