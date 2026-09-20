"""
controller/telemetry.py — Stage 2: Telemetry Collector

Roadmap Tier 1 #4 ("Stats requests") + Tier 2 #2 ("OpenFlow Statistics
Collection"): a periodic polling loop (hub.spawn, not ad-hoc requests),
deriving throughput/loss from counter DELTAS between two polls, not a
single snapshot.

What this does, once per second per connected switch:
  1. Sends OFPPortStatsRequest to every connected datapath.
  2. On the reply, diffs rx/tx byte and packet counters against the
     previous poll to get throughput (Mbps) and a loss proxy.
  3. Runs a lightweight latency/jitter probe between switch pairs using
     OpenFlow echo request/reply round-trip time (a stand-in for real
     inter-switch probe packets — swap in LLDP-timestamp or dedicated
     probe packets later if the echo RTT proves too coarse).
  4. Writes one row per (switch, port) reading to Log A via
     evaluation/logger.py, using flow_id="link:<dpid>:<port>" until
     Stage 3 (classifier) exists to attribute readings to real flows.

This is intentionally decoupled from main_app.py's packet_in logic —
telemetry only reads stats, it never installs flow-mods, so it can't
interfere with the week 1-2 learning-switch behavior.
"""

import os
import sys
import time
from collections import defaultdict

from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from evaluation.logger import FlowMetricsLogger  # noqa: E402

POLL_INTERVAL_SEC = 1.0


class TelemetryCollector:
    def __init__(self, ryu_app, method="adaptiveqos", scenario="unlabeled"):
        self.app = ryu_app
        self.logger_csv = FlowMetricsLogger(method=method, scenario=scenario)

        # Previous poll's raw counters, for delta computation.
        # {(dpid, port_no): {"rx_bytes":.., "tx_bytes":.., "ts":..}}
        self._prev_port_stats = {}

        # Echo RTT samples per dpid, for a running jitter estimate.
        # {dpid: [rtt_ms, ...]} — capped ring buffer, see _record_rtt.
        self._rtt_samples = defaultdict(list)
        self._echo_sent_at = {}

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        while True:
            for dpid, datapath in list(self.app.datapaths.items()):
                self._request_port_stats(datapath)
                self._send_echo_probe(datapath)
            hub.sleep(POLL_INTERVAL_SEC)

    # ------------------------------------------------------------------

    # Port stats -> throughput / loss
    # ------------------------------------------------------------------
    def _request_port_stats(self, datapath):
        parser = datapath.ofproto_parser
        ofp = datapath.ofproto
        req = parser.OFPPortStatsRequest(datapath, 0, ofp.OFPP_ANY)
        datapath.send_msg(req)

    def handle_port_stats_reply(self, ev):
        """Wire this up in main_app.py with:
        @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
        def _port_stats_reply(self, ev):
            self.telemetry.handle_port_stats_reply(ev)
        (kept out of main_app.py's decorator list for week 1-2 so the
        learning-switch milestone stays minimal; wire in for week 3-4.)
        """
        dpid = ev.msg.datapath.id
        now = time.time()

        for stat in ev.msg.body:
            port_no = stat.port_no
            key = (dpid, port_no)
            prev = self._prev_port_stats.get(key)

            if prev is not None:
                dt = now - prev["ts"]
                if dt < 0.1:
                    # Two stats replies for the same port arrived within
                    # 100ms of each other (burst delivery / timing jitter
                    # in the poll loop) — dividing bytes by a near-zero
                    # dt here produces physically impossible throughput
                    # spikes (thousands of Mbps on an 8-10Mbit link).

                    # Skip this reading rather than log garbage; the next
                    # ~1s-spaced poll will produce a sane one.
                    self._prev_port_stats[key] = {
                        "rx_bytes": stat.rx_bytes, "tx_bytes": stat.tx_bytes,
                        "rx_packets": stat.rx_packets, "tx_packets": stat.tx_packets,
                        "rx_dropped": stat.rx_dropped, "tx_dropped": stat.tx_dropped,
                        "ts": now,
                    }
                    continue

                rx_bytes_delta = stat.rx_bytes - prev["rx_bytes"]
                tx_bytes_delta = stat.tx_bytes - prev["tx_bytes"]
                rx_dropped_delta = stat.rx_dropped - prev["rx_dropped"]
                tx_dropped_delta = stat.tx_dropped - prev["tx_dropped"]

                throughput_mbps = ((rx_bytes_delta + tx_bytes_delta) * 8 / 1e6) / dt

                total_pkts_delta = max(
                    (stat.rx_packets - prev["rx_packets"])
                    + (stat.tx_packets - prev["tx_packets"]),
                    0,
                )
                dropped_delta = max(rx_dropped_delta + tx_dropped_delta, 0)
                loss_pct = (
                    100.0 * dropped_delta / (total_pkts_delta + dropped_delta)
                    if (total_pkts_delta + dropped_delta) > 0
                    else 0.0
                )

                latency_ms, jitter_ms = self._current_latency_jitter(dpid)

                self.logger_csv.write_row(

                    flow_id=f"link:{dpid}:{port_no}",
                    flow_type="unclassified",  # Stage 3 fills this in, week 5-6
                    path_chosen=f"dpid={dpid}",
                    latency_ms=latency_ms,
                    jitter_ms=jitter_ms,
                    packet_loss_pct=round(loss_pct, 3),
                    throughput_mbps=round(throughput_mbps, 3),
                )

            self._prev_port_stats[key] = {
                "rx_bytes": stat.rx_bytes,
                "tx_bytes": stat.tx_bytes,
                "rx_packets": stat.rx_packets,
                "tx_packets": stat.tx_packets,
                "rx_dropped": stat.rx_dropped,
                "tx_dropped": stat.tx_dropped,
                "ts": now,
            }

    # ------------------------------------------------------------------
    # Latency / jitter via echo request/reply RTT
    # ------------------------------------------------------------------
    def _send_echo_probe(self, datapath):
        parser = datapath.ofproto_parser
        self._echo_sent_at[datapath.id] = time.time()
        req = parser.OFPEchoRequest(datapath, data=b"adaptiveqos-probe")
        datapath.send_msg(req)

    def handle_echo_reply(self, ev):
        """Wire this up in main_app.py with:
        @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
        def _echo_reply(self, ev):

            self.telemetry.handle_echo_reply(ev)
        """
        dpid = ev.msg.datapath.id
        sent_at = self._echo_sent_at.get(dpid)
        if sent_at is None:
            return
        rtt_ms = (time.time() - sent_at) * 1000.0
        self._record_rtt(dpid, rtt_ms)

    def _record_rtt(self, dpid, rtt_ms, window=20):
        samples = self._rtt_samples[dpid]
        samples.append(rtt_ms)
        if len(samples) > window:
            samples.pop(0)

    def _current_latency_jitter(self, dpid):
        samples = self._rtt_samples.get(dpid, [])
        if not samples:
            return 0.0, 0.0
        latency_ms = sum(samples) / len(samples)
        if len(samples) < 2:
            return round(latency_ms, 3), 0.0
        # Jitter per RFC 3550 style: mean absolute deviation between
        # consecutive samples (simple and defensible in a viva; swap
        # for the exact RFC 3550 smoothed formula if asked to justify
        # it more rigorously).
        diffs = [abs(samples[i] - samples[i - 1]) for i in range(1, len(samples))]
        jitter_ms = sum(diffs) / len(diffs)
        return round(latency_ms, 3), round(jitter_ms, 3)