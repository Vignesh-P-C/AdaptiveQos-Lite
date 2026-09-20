# AdaptiveQoS-Lite

Lightweight online-learning adaptive routing for jitter-sensitive traffic
in SDN. This repo covers **weeks 1-4** of the 10-week plan: testbed +
controller hello world, telemetry collector, static ECMP baseline,
traffic generator + classifier.

## Environment setup

Run this on a real Ubuntu machine or VM (20.04/22.04 recommended) with
root — Mininet needs actual kernel network namespaces, which a
container/CI sandbox typically can't grant.

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iperf3

python3 -m venv .venv
source .venv/bin/activate

# Ryu (last released 2021) breaks on modern setuptools — pin setuptools
# down BEFORE installing ryu, in the same venv:
pip install "setuptools<58" wheel
pip install -r requirements.txt
```

If `ryu-manager` still fails on import with an `eventlet`/`dnspython`
error, `pip install "eventlet==0.30.2" "dnspython<2.0"` inside the venv
— this is a known ryu/eventlet compatibility trap, not a bug in this
code.

## Run order (week 1-2 milestone)

Two terminals, same venv:

```bash
# terminal 1
ryu-manager controller/main_app.py

# terminal 2
sudo python3 topology/topo.py
```

You should see: switch-connected log lines in terminal 1 (one per
switch, OpenFlow handshake), then `pingall` in terminal 2 reporting
`0% dropped` across h1/h2. That's the whole week 1-2 goal — don't move
on until this is clean.

## Run order (week 3-4: telemetry + traffic + ECMP baseline)

With the above running, generate traffic and watch Log A fill in.
Mininet's `py` only evaluates expressions — `import` needs `px`, and
the repo root needs adding to `sys.path` since scripts run from inside
`topology/`:

```bash
# in the Mininet CLI (terminal 2, after topo.py drops you into it)
mininet> px import sys, os
mininet> px sys.path.insert(0, os.path.abspath("."))
mininet> px from traffic_gen.generate_traffic import run_scenario
mininet> py run_scenario(net, "moderate", duration_sec=60)
```

`run_scenario()` blocks for the full `duration_sec` (it also paces a
real h1<->h2 ping-based latency/jitter sample once a second while
iperf3's flows run in the background) and returns once done — no need
to wait manually. After it returns:

```bash
mininet> h1 pkill iperf3
mininet> h2 pkill iperf3
```

before starting the next scenario. Each scenario needs a **fresh
controller + fresh topology** — `ADAPTIVEQOS_SCENARIO` is read once at
controller startup, so running two scenarios against the same
long-lived `ryu-manager` process silently mixes their data into one
file. Per scenario:

```bash
# terminal 1
ADAPTIVEQOS_SCENARIO=<light|moderate|heavy> ADAPTIVEQOS_STP=0 ryu-manager baselines/static_ecmp_only.py
# terminal 2
sudo ADAPTIVEQOS_STP=0 python3 topology/topo.py
```

Watch `data/raw/flow_metrics_*.csv` grow — that's `telemetry.py`'s
polling loop writing Log A once a second per switch port (throughput
and packet-loss-pct only; see the note below on latency/jitter). This
is the main hello-world of week 3-4: telemetry running against real
generated traffic.

To run the **static ECMP baseline** instead of the learning-switch
controller (for the eventual Section 8 comparison table), swap which
Ryu app you launch in terminal 1:

```bash
ryu-manager baselines/static_ecmp_only.py
```

Everything else (`topo.py`, `generate_traffic.py`) is unchanged — same
topology, same traffic, different controller. That's what makes the
later ECMP-vs-AdaptiveQoS-Lite comparison a fair one.

## Known issue: `telemetry.py`'s latency/jitter columns

`telemetry.py`'s `latency_ms`/`jitter_ms` (on the `link:<dpid>:<port>`
rows) come from OpenFlow echo request/reply — the RTT between the Ryu
controller and each switch's **control channel**, both on localhost.
That's not the real h1<->h2 data-path experience and doesn't scale
with congestion, which showed up as jitter/latency *decreasing* from
light to heavy scenarios. `throughput_mbps` and `packet_loss_pct` on
those rows are unaffected (from real port-counter deltas), so keep
using them.

For real latency/jitter, `traffic_gen/generate_traffic.py`'s
`run_scenario()` now also pings h1->h2 once a second for the scenario's
duration and logs it to the same file under `flow_id=h1-h2-realtime`.
Use those rows for latency/jitter in analysis; use the `link:*` rows
for throughput/loss. (Caveat: this ping is ICMP, so it gets its own
ECMP hash and may not always land on the same path as the real UDP
video flow — a fine proxy for now, worth a one-line note in the report
if asked.)

## What's built (weeks 1-4) vs. what's next (weeks 5+)

| Done now | File | Not yet (by design — later weeks) |
|---|---|---|
| Testbed topology, redundant paths | `topology/topo.py` | scaling to full 10-15 switch campus topo (Objective 1) |
| Learning-switch controller + OpenFlow handshake | `controller/main_app.py` | Stage 4 agent wiring into packet_in (week 5-6) |
| Telemetry: port-stats polling, echo-RTT latency/jitter, Log A | `controller/telemetry.py` | per-flow (not per-link) attribution — needs classifier wired into main_app.py first |
| Flow classifier (DSCP + UDP port heuristics) | `controller/classifier.py` | wiring into `main_app.py`'s routing decision (week 5-6) |
| Static ECMP baseline, standalone | `baselines/static_ecmp_only.py` | — this one's actually complete for its scope |
| ECMP path math (shared) | `controller/ecmp_fallback.py` | — |
| Traffic generator (real-time UDP + bulk TCP, 3 scenarios) | `traffic_gen/generate_traffic.py` | D-ITG variant (iperf3-only for now, sufficient per build plan) |
| Log A/B/C CSV writers | `evaluation/logger.py` | `analyze_results.py`, `run_experiment.py` (week 7-8) |
| — | `controller/agent.py` | **not started** — week 5-6, the actual novelty (bandit/Q-learning) |
| — | `dashboard/` | **not started** — week 9 |

Log B (`agent_internal_log.csv`) and Log C (`setup_cost_log.csv`)
writers exist in `evaluation/logger.py` already, ahead of schedule,
since `static_ecmp_only.py` needs Log C now and `agent.py` will need
Log B as soon as it's written — no reason to touch `logger.py` again
later.

## Sanity-checked without Mininet/Ryu installed

`controller/ecmp_fallback.py`, `controller/classifier.py`'s
`classify_flow()`, and `evaluation/logger.py` have no Ryu/Mininet
dependency and were exercised directly:
- ECMP hashing confirmed deterministic per-flow and spread across both
  redundant `s1->s4` paths on the default topology.
- Classifier heuristics confirmed: DSCP EF/AF4x and RTP-range UDP both
  tag `real_time`; everything else tags `best_effort`.
- All three CSV loggers write header + rows matching the Build Plan §2
  schemas exactly.

Everything that touches `ryu` or `mininet` (topology build, controller
apps, telemetry's OpenFlow message handling) is syntax-checked but
**needs to be run on your VM** to confirm end-to-end — that part of
week 1-2's "hello world" is on you to execute and watch for the
`pingall` result.

## Next session

Week 5-6: `controller/agent.py` — the actual novelty. Tabular
Q-learning / multi-armed bandit over candidate paths per real-time
flow, reward = negative observed jitter, wired into `main_app.py`'s
packet_in so real-time flows (per `classifier.py`) get routed by the
agent instead of falling through to the learning-switch default.