"""evaluation/analyze_results.py -- Log A/B/C CSVs -> IDF table + figures.

Run after evaluation/run_all.sh (or any manual set of runs):
    python3 evaluation/analyze_results.py
Writes data/processed/performance_summary.csv and four PNGs in
data/figures/. Regenerate from the CSVs, never hand-edit the numbers.
"""

import glob
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RAW = os.path.join(ROOT, "data", "raw")
PROCESSED = os.path.join(ROOT, "data", "processed")
FIGURES = os.path.join(ROOT, "data", "figures")


def load_flow_metrics():
    files = glob.glob(os.path.join(RAW, "flow_metrics_*.csv"))
    if not files:
        raise SystemExit("No flow_metrics CSVs in data/raw/ -- run evaluation/run_all.sh first.")
    return pd.concat((pd.read_csv(f) for f in files), ignore_index=True)


def load_optional(name, columns):
    path = os.path.join(RAW, name)
    if not os.path.exists(path):
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path)


def realtime_rows(df):
    """h1<->h2 ping-based rows -- the only reliable jitter/latency source.
    telemetry.py's link:* rows use echo-RTT on the controller<->switch
    control channel, which counterintuitively drops under congestion
    (see README "Known issue"); those rows are throughput/loss only."""
    return df[df["flow_id"] == "h1-h2-realtime"]


def link_rows(df):
    return df[df["flow_id"].astype(str).str.startswith("link:")]


# No veth-backed emulated link in this testbed can plausibly exceed this;
# an occasional bogus OVS port-stats delta (observed once per ~600 samples,
# likely a stats-parsing glitch under this environment's Python 3.14 +
# os_ken combo, since dt itself checks out normal at ~1s -- the byte-delta
# read back was simply garbage) has produced single-row values in the
# hundreds-of-trillions of Mbps, which a raw mean lets dominate the whole
# scenario average. Drop those rather than change the statistic (median
# across all link rows collapses toward zero, since most switch ports are
# idle at any given instant -- it hides real throughput just as badly).
PLAUSIBLE_THROUGHPUT_MBPS_MAX = 100_000


def performance_table(df):
    lat_jit = realtime_rows(df).groupby(["scenario", "method"]).agg(
        jitter_ms=("jitter_ms", "mean"),
        latency_ms=("latency_ms", "mean"),
    )
    links = link_rows(df)
    dropped = links[links["throughput_mbps"] > PLAUSIBLE_THROUGHPUT_MBPS_MAX]
    if len(dropped):
        print(f"Dropping {len(dropped)} implausible throughput sample(s) "
              f"(> {PLAUSIBLE_THROUGHPUT_MBPS_MAX} Mbps) before averaging:")
        print(dropped[["scenario", "method", "flow_id", "throughput_mbps"]].to_string(index=False))
    links = links[links["throughput_mbps"] <= PLAUSIBLE_THROUGHPUT_MBPS_MAX]
    loss_tput = links.groupby(["scenario", "method"]).agg(
        packet_loss_pct=("packet_loss_pct", "mean"),
        throughput_mbps=("throughput_mbps", "mean"),
    )
    table = lat_jit.join(loss_tput, how="outer").reset_index()
    os.makedirs(PROCESSED, exist_ok=True)
    table.to_csv(os.path.join(PROCESSED, "performance_summary.csv"), index=False)
    print(table.to_string(index=False))
    return table


def figure5_jitter_bar(table):
    pivot = table.pivot(index="scenario", columns="method", values="jitter_ms")
    ax = pivot.plot(kind="bar", figsize=(7, 4))
    ax.set_ylabel("Mean jitter (ms, h1<->h2 ping)")
    ax.set_title("Jitter by scenario and method")
    plt.tight_layout()
    plt.savefig(os.path.join(FIGURES, "figure5_jitter_bar.png"))
    plt.close()


def figure6_latency_line(df, scenario="heavy"):
    subset = realtime_rows(df)
    subset = subset[subset["scenario"] == scenario]
    if subset.empty:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    for method, g in subset.groupby("method"):
        g = g.sort_values("timestamp")
        t0 = g["timestamp"].iloc[0]
        ax.plot(g["timestamp"] - t0, g["latency_ms"], label=method)
    ax.set_xlabel("Seconds into run")
    ax.set_ylabel("Latency (ms)")
    ax.set_title("Latency over time -- %s scenario" % scenario)
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(FIGURES, "figure6_latency_line.png"))
    plt.close()


def figure7_setup_cost_bar(cost_df):
    if cost_df.empty:
        return
    avg = cost_df.groupby("method")["wall_clock_seconds"].mean()
    ax = avg.plot(kind="bar", figsize=(6, 4), logy=True)
    ax.set_ylabel("Setup time, seconds (log scale)")
    ax.set_title("Setup/training cost by method")
    plt.tight_layout()
    plt.savefig(os.path.join(FIGURES, "figure7_setup_cost_bar.png"))
    plt.close()


def figure8_convergence(agent_df, window=20):
    if agent_df.empty:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    for scenario, g in agent_df.groupby("scenario"):
        g = g.sort_values("timestamp")
        rolling = g["reward"].rolling(window, min_periods=1).mean()
        t0 = g["timestamp"].iloc[0]
        ax.plot(g["timestamp"] - t0, rolling, label=scenario)
    ax.set_xlabel("Seconds into run")
    ax.set_ylabel("Reward (rolling mean, window=%d)" % window)
    ax.set_title("Agent convergence")
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(FIGURES, "figure8_convergence.png"))
    plt.close()


def main():
    os.makedirs(FIGURES, exist_ok=True)
    df = load_flow_metrics()
    table = performance_table(df)

    cost_df = load_optional("setup_cost_log.csv", ["method", "wall_clock_seconds"])
    agent_df = load_optional("agent_internal_log.csv", ["timestamp", "scenario", "reward"])

    figure5_jitter_bar(table)
    figure6_latency_line(df)
    figure7_setup_cost_bar(cost_df)
    figure8_convergence(agent_df)
    print("Figures written to data/figures/, table to data/processed/performance_summary.csv")


if __name__ == "__main__":
    main()
