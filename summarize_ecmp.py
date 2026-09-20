"""Summarize the latest ECMP run per scenario (light / moderate / heavy).

Run from the repo root:  python3 summarize_ecmp.py
Pairs each scenario's newest ping file (h1-h2-realtime rows, written by
traffic_gen) with the Ryu telemetry file that started just before it, and
only averages the telemetry rows inside the ping run's time window.
"""
import glob
import os

import pandas as pd


def stamp(path):
    return os.path.basename(path).rsplit("_", 1)[1][:-4]


for scenario in ["light", "moderate", "heavy"]:
    files = sorted(glob.glob(f"data/raw/flow_metrics_{scenario}_ecmp_*.csv"))
    frames = {f: pd.read_csv(f) for f in files}

    ping_f = [f for f, d in frames.items() if (d["flow_id"] == "h1-h2-realtime").any()][-1]
    tele_f = [f for f, d in frames.items()
              if d["flow_id"].str.startswith("link:").any() and stamp(f) < stamp(ping_f)][-1]

    rt = frames[ping_f]
    rt = rt[rt["flow_id"] == "h1-h2-realtime"]
    t0, t1 = rt["timestamp"].min(), rt["timestamp"].max()

    link = frames[tele_f]
    link = link[link["flow_id"].str.startswith("link:") & ~link["flow_id"].str.contains("4294967294")]
    link = link[(link["timestamp"] >= t0) & (link["timestamp"] <= t1)]
    link = link[link["throughput_mbps"] > 0]

    print(f"--- {scenario} ---")
    print(f"  ping file:      {ping_f}  ({len(rt)} pings)")
    print(f"  telemetry file: {tele_f}  ({len(link)} active-port rows in window)")
    print(f"  avg latency_ms (ping):  {rt['latency_ms'].mean():.3f}")
    print(f"  avg jitter_ms (ping):   {rt['jitter_ms'].mean():.3f}")
    print(f"  ping loss %:            {rt['packet_loss_pct'].mean():.3f}")
    print(f"  avg throughput_mbps:    {link['throughput_mbps'].mean():.3f}")
    print(f"  avg port drop %:        {link['packet_loss_pct'].mean():.3f}")
