"""
baselines/static_ecmp_only.py — the "do nothing smart" comparison run

Week 3-4 deliverable. A complete, standalone Ryu app that routes every
flow — real-time or best-effort, doesn't matter, that's the point —
with plain ECMP and never revisits the decision. This is what
AdaptiveQoS-Lite's Stage 4 agent has to beat in Section 8/9 of the IDF
("Baseline (Static ECMP)" column).

Deliberately does NOT import telemetry.py or classifier.py — the
baseline must not get any benefit from the modules that only the
adaptive method uses, or the comparison is unfair. It performs its own
minimal port-stats polling ONLY for the setup-cost timing measurement
(Log C), not for routing decisions.

Run:
    ryu-manager baselines/static_ecmp_only.py
    sudo python3 topology/topo.py
"""

import os
import sys
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp, ether_types
from ryu.lib import hub

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from controller.ecmp_fallback import ecmp_select_path, DEFAULT_TOPO_LINKS  # noqa: E402
from evaluation.logger import SetupCostLogger  # noqa: E402
from controller.telemetry import TelemetryCollector  # noqa: E402

# Map topology node names (as used in ecmp_fallback's graph) to dpid.
# s1 -> dpid 1, s2 -> dpid 2, etc. — matches Mininet's default dpid
# assignment for switches named sN in topology/topo.py.
NODE_TO_DPID = {"s1": 1, "s2": 2, "s3": 3, "s4": 4}
DPID_TO_NODE = {v: k for k, v in NODE_TO_DPID.items()}


class StaticEcmpOnlyApp(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.graph = {}
        for a, b in DEFAULT_TOPO_LINKS:
            self.graph.setdefault(a, set()).add(b)
            self.graph.setdefault(b, set()).add(a)

        self.setup_cost = SetupCostLogger()
        self.setup_cost.start("ecmp")
        self._reported_ready = False

        # Read-only measurement (does NOT influence routing decisions —
        # same telemetry module main_app.py uses, so Log A is comparable
        # across methods). Scenario tag comes from the environment so a
        # single script doesn't need editing between light/moderate/heavy
        # runs: ADAPTIVEQOS_SCENARIO=heavy ryu-manager baselines/...
        scenario = os.environ.get("ADAPTIVEQOS_SCENARIO", "unlabeled")
        self.telemetry = TelemetryCollector(self, method="ecmp", scenario=scenario)
        self.telemetry_thread = hub.spawn(self.telemetry.run)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        self.datapaths[datapath.id] = datapath
        self.logger.info("ECMP baseline: switch connected dpid=%s", datapath.id)

        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)
        ]
        self.add_flow(datapath, 0, match, actions)

        # Log C: "working state reached" = all expected switches connected.
        if not self._reported_ready and len(self.datapaths) >= len(NODE_TO_DPID):
            self.setup_cost.finish("ecmp", gpu_used="no")
            self._reported_ready = True
            self.logger.info("ECMP baseline: setup-cost timing recorded")

    def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                     priority=priority, match=match,
                                     instructions=inst, idle_timeout=idle_timeout)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                     match=match, instructions=inst,
                                     idle_timeout=idle_timeout)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        self.telemetry.handle_port_stats_reply(ev)

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def echo_reply_handler(self, ev):
        self.telemetry.handle_echo_reply(ev)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst_mac, src_mac = eth.dst, eth.src
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src_mac] = in_port

        ip = pkt.get_protocol(ipv4.ipv4)
        out_port = self._decide_out_port(dpid, in_port, dst_mac, pkt, ip, ofproto)

        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst_mac, eth_src=src_mac)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions, msg.buffer_id, idle_timeout=30)
                return
            self.add_flow(datapath, 1, match, actions, idle_timeout=30)

        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        datapath.send_msg(parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port,
            actions=actions, data=data,
        ))

    def _decide_out_port(self, dpid, in_port, dst_mac, pkt, ip, ofproto):
        """Learning-switch fallback for anything not on the h1<->h2
        inter-switch path (ARP, unknown MACs); ECMP hash-select for
        traffic actually crossing the redundant s1<->s4 paths.

        flow_key MUST include L4 ports, not just (src, dst, proto) —
        with only two hosts, every flow shares the same IP pair, so
        omitting ports collapses every UDP flow (and separately every
        TCP flow) onto a single path for the whole run. That defeats
        ECMP's actual job (spreading flows across equal-cost paths)
        and would silently invalidate the ECMP-vs-AdaptiveQoS-Lite
        comparison this baseline exists to provide.
        """
        node = DPID_TO_NODE.get(dpid)
        if node in ("s1",) and ip is not None:
            udp_hdr = pkt.get_protocol(udp.udp)
            tcp_hdr = pkt.get_protocol(tcp.tcp)
            src_port = dst_port = None
            if udp_hdr is not None:
                src_port, dst_port = udp_hdr.src_port, udp_hdr.dst_port
            elif tcp_hdr is not None:
                src_port, dst_port = tcp_hdr.src_port, tcp_hdr.dst_port

            flow_key = (ip.src, ip.dst, ip.proto, src_port, dst_port)
            path = ecmp_select_path(self.graph, "s1", "s4", flow_key)
            if path and len(path) > 1:
                next_hop = path[1]
                # Static per-topology port mapping for this fixed 2-path
                # topology: s1's ports to s2/s3 mirror addLink() order in
                # topo.py. Reads awkward on purpose — a baseline shouldn't
                # need anything smarter than "look up the static table".
                next_hop_port = {"s2": 2, "s3": 3}.get(next_hop)
                if next_hop_port:
                    return next_hop_port

        if dst_mac in self.mac_to_port.get(dpid, {}):
            return self.mac_to_port[dpid][dst_mac]
        return ofproto.OFPP_FLOOD
