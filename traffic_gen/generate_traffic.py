"""
traffic_gen/generate_traffic.py — Stage: traffic generation

Week 3-4 deliverable. Wraps iperf3 to produce the two traffic classes
Section 8's scenarios need:

  * real-time-like traffic: UDP, fixed low bitrate, small packets,
    on a port inside classifier.py's REALTIME_UDP_PORTS range — this
    is what the classifier tags real_time and (later) the agent routes.
  * best-effort bulk traffic: TCP, throughput-test style, to actually
    saturate a path and give the agent/ECMP something to react to.

Meant to run from the Mininet CLI/script, i.e. h1 and h2 already exist
as Mininet hosts. Two ways to use it:

  1. From within a Mininet script (topology/topo.py's CLI, or your own
     driver): import and call run_scenario(net, "moderate").
  2. Directly on a host's shell (if you're driving h1/h2 by hand):
     python3 generate_traffic.py server        # run on h2
     python3 generate_traffic.py client moderate --server-ip 10.0.0.2   # on h1

Scenario definitions (Section 8 / Build Plan): light / moderate / heavy,
distinguished by how much best-effort bulk traffic competes with the
real-time flow. Real-time traffic itself stays constant across
scenarios — congestion should come from the environment, not from the
thing being measured.
"""

import argparse
import re
import subprocess
import time

# Keep in sync with controller/classifier.py's REALTIME_UDP_PORTS.
REALTIME_UDP_PORT = 16386
BULK_TCP_PORT = 5201  # iperf3 default

REALTIME_BITRATE = "1M"       # steady low-bitrate UDP, video/VoIP-like
REALTIME_PACKET_LEN_BYTES = 160  # small frequent packets, voice-like

SCENARIOS = {
    # name: (num_bulk_tcp_flows, bulk_flow_bandwidth_cap)
    "light": (0, None),
    "moderate": (1, None),       # one TCP flow, uncapped -> saturates one path
    "heavy": (3, None),          # multiple concurrent TCP flows across paths
}


def start_iperf_server(port=BULK_TCP_PORT):
    """Run on the receiving host (h2). Blocks — run with & or in its
    own Mininet host.cmd() call."""
    return subprocess.Popen(
        ["iperf3", "-s", "-p", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def start_realtime_udp_flow(server_ip, duration_sec=60, port=REALTIME_UDP_PORT):
    """Client side: fixed-bitrate small-packet UDP flow simulating a
    video/VoIP call. -b caps bitrate, -l sets packet length."""
    cmd = [
        "iperf3", "-c", server_ip, "-u",
        "-b", REALTIME_BITRATE,
        "-l", str(REALTIME_PACKET_LEN_BYTES),
        "-t", str(duration_sec),
        "-p", str(port),
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_bulk_tcp_flow(server_ip, duration_sec=60, port=BULK_TCP_PORT, bandwidth=None):
    """Client side: TCP throughput-test flow, best-effort bulk traffic."""
    cmd = ["iperf3", "-c", server_ip, "-t", str(duration_sec), "-p", str(port)]
    if bandwidth:
        cmd += ["-b", bandwidth]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def sample_realtime_latency(net, method, scenario_name, duration_sec=60,
                             server_ip="10.0.0.2", interval=1.0):
    """Real h1->h2 latency/jitter, sampled from the Mininet side (which
    has host access) instead of telemetry.py's OFPEchoRequest/Reply RTT,
    which only measures the controller<->switch control channel (both
    on localhost) and never scales with real traffic/congestion -- a
    Week 3-4 bug found while validating the ECMP baseline's numbers.

    One row per ping to its own Log A file (flow_id="h1-h2-realtime"),
    via the same evaluation.logger.FlowMetricsLogger used elsewhere so
    it stays in the same schema. Blocks for duration_sec.
    """
    from evaluation.logger import FlowMetricsLogger

    h1 = net.get("h1")
    logger = FlowMetricsLogger(method=method, scenario=scenario_name)
    rtts = []
    n = max(1, int(duration_sec / interval))
    try:
        for _ in range(n):
            loop_start = time.time()
            out = h1.cmd(f"ping -c 1 -W 1 {server_ip}")
            m = re.search(r"time=([\d.]+)", out)
            if m:
                rtt = float(m.group(1))
                rtts.append(rtt)
                jitter = abs(rtts[-1] - rtts[-2]) if len(rtts) >= 2 else 0.0
                logger.write_row(
                    flow_id="h1-h2-realtime", flow_type="real_time",
                    path_chosen="measured", latency_ms=rtt,
                    jitter_ms=round(jitter, 3), packet_loss_pct=0.0,
                    throughput_mbps=0.0,
                )
            else:
                logger.write_row(
                    flow_id="h1-h2-realtime", flow_type="real_time",
                    path_chosen="measured", latency_ms="", jitter_ms="",
                    packet_loss_pct=100.0, throughput_mbps=0.0,
                )
            # ping -c1 -W1 returns in a few ms on success (the -W1
            # timeout only applies on loss) -- without this, all `n`
            # samples fire back-to-back in well under a second and
            # duration_sec is not actually honored.
            elapsed = time.time() - loop_start
            time.sleep(max(0, interval - elapsed))
    finally:
        logger.close()


def run_scenario(net, scenario_name, duration_sec=60, server_ip="10.0.0.2",
                  method="ecmp"):
    """Drive the scenario from inside a Mininet script: net is the
    Mininet object from topology/topo.py's build_net(). Client/server
    processes are started via host.cmd() with a trailing '&' (Mininet's
    normal backgrounding pattern) rather than returned as Popen handles
    — the caller waits out duration_sec, then explicitly kills iperf3
    on both hosts (h1 pkill iperf3 / h2 pkill iperf3) before starting
    the next scenario. Returns a small metadata dict, not process handles.

    Note on realism: each concurrent bulk flow here uses a distinct
    TCP port to avoid port collisions when heavy uses 3 flows.
    """
    if scenario_name not in SCENARIOS:
        raise ValueError(f"Unknown scenario '{scenario_name}', choose from {list(SCENARIOS)}")
    num_bulk_flows, bw_cap = SCENARIOS[scenario_name]

    h1, h2 = net.get("h1"), net.get("h2")

    # Kill any iperf3 left running from a previous scenario call that
    # wasn't cleaned up — otherwise "-s -p <port> -D" fails to bind
    # ("address already in use") and this scenario silently runs with
    # no server on that port.
    h1.cmd("pkill -9 -f iperf3 2>/dev/null")
    h2.cmd("pkill -9 -f iperf3 2>/dev/null")
    time.sleep(0.5)

    # One iperf3 server per port in use: realtime UDP port + one TCP
    # port per bulk flow (iperf3 servers are single-port).
    h2.cmd(f"iperf3 -s -p {REALTIME_UDP_PORT} -D")
    for i in range(num_bulk_flows):
        h2.cmd(f"iperf3 -s -p {BULK_TCP_PORT + i} -D")
    time.sleep(1)  # let servers bind before clients connect

    rt_cmd = (
        f"iperf3 -c {server_ip} -u -b {REALTIME_BITRATE} "
        f"-l {REALTIME_PACKET_LEN_BYTES} -t {duration_sec} "
        f"-p {REALTIME_UDP_PORT} &"
    )
    h1.cmd(rt_cmd)

    for i in range(num_bulk_flows):
        bulk_cmd = f"iperf3 -c {server_ip} -t {duration_sec} -p {BULK_TCP_PORT + i}"
        if bw_cap:
            bulk_cmd += f" -b {bw_cap}"
        h1.cmd(bulk_cmd + " &")

    # Blocks for duration_sec, sampling real h1->h2 latency/jitter via
    # ping while iperf3's flows run in the background above. This is
    # also what makes the call as a whole synchronous now -- it used to
    # return immediately since iperf3 itself is backgrounded with '&'.
    sample_realtime_latency(net, method, scenario_name, duration_sec=duration_sec,
                             server_ip=server_ip)

    return {"scenario": scenario_name, "duration_sec": duration_sec,
            "num_bulk_flows": num_bulk_flows}


def _cli():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    server_p = sub.add_parser("server", help="run iperf3 servers (run on h2)")
    server_p.add_argument("--bulk-flows", type=int, default=3,
                           help="max concurrent bulk TCP servers to open ports for")

    client_p = sub.add_parser("client", help="drive a scenario (run on h1)")
    client_p.add_argument("scenario", choices=list(SCENARIOS))
    client_p.add_argument("--server-ip", default="10.0.0.2")
    client_p.add_argument("--duration", type=int, default=60)

    args = parser.parse_args()

    if args.mode == "server":
        procs = [start_iperf_server(REALTIME_UDP_PORT)]
        for i in range(args.bulk_flows):
            procs.append(start_iperf_server(BULK_TCP_PORT + i))
        print(f"Started {len(procs)} iperf3 servers. Ctrl-C to stop.")
        try:
            for p in procs:
                p.wait()
        except KeyboardInterrupt:
            for p in procs:
                p.terminate()

    elif args.mode == "client":
        num_bulk_flows, bw_cap = SCENARIOS[args.scenario]
        procs = [start_realtime_udp_flow(args.server_ip, args.duration)]
        for i in range(num_bulk_flows):
            procs.append(start_bulk_tcp_flow(
                args.server_ip, args.duration, BULK_TCP_PORT + i, bw_cap
            ))
        print(f"Scenario '{args.scenario}': 1 real-time flow + "
              f"{num_bulk_flows} bulk flow(s) for {args.duration}s")
        for p in procs:
            p.wait()


if __name__ == "__main__":
    _cli()