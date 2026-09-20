
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

FIXED VERSION: forwarding is now destination-IP based and direction
aware, and nothing is ever flooded (the topology has a loop, so flooding
causes storms). Traffic h1->h2 and h2->h1 are both handled:
  * at an edge switch (s1 / s4) the packet either goes to the local host
    (if the destination is attached here) or crosses the core, where the
    ECMP hash picks s2 or s3;
  * at a middle switch (s2 / s3) the packet is sent toward the edge
    switch that owns the destination.
ARP is forwarded the same way (by its target IP) instead of being flooded.

!! Port numbers below must match topology/topo.py. Verify them at the
!! mininet> prompt with the `net` command and edit the four tables
!! if they differ.

Run:
    ryu-manager baselines/static_ecmp_only.py
    sudo python3 topology/topo.py
"""

import os
import sys

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, arp, tcp, udp, ether_types
from ryu.lib import hub

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from controller.ecmp_fallback import ecmp_select_path, DEFAULT_TOPO_LINKS  # noqa: E402
from evaluation.logger import SetupCostLogger  # noqa: E402
from controller.telemetry import TelemetryCollector  # noqa: E402

# Map topology node names (as used in ecmp_fallback's graph) to dpid.
NODE_TO_DPID = {"s1": 1, "s2": 2, "s3": 3, "s4": 4}
DPID_TO_NODE = {v: k for k, v in NODE_TO_DPID.items()}

# ---- Static tables for this fixed topology (verify with `net`) ----------
# Which switch (dpid) each host IP is attached to.
HOST_SWITCH = {"10.0.0.1": 1, "10.0.0.2": 4}
# Port on that switch that faces the host (s1 -> h1, s4 -> h2).
HOST_PORT = {1: 1, 4: 1}
# On an edge switch: port toward each middle switch.
EDGE_PORTS = {1: {"s2": 2, "s3": 3}, 4: {"s2": 2, "s3": 3}}
# On a middle switch: port toward each edge switch (keyed by edge dpid).
MIDDLE_PORTS = {2: {1: 1, 4: 2}, 3: {1: 1, 4: 2}}
# -------------------------------------------------------------------------


class StaticEcmpOnlyApp(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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

        ip = pkt.get_protocol(ipv4.ipv4)
        arp_pkt = pkt.get_protocol(arp.arp)

        if ip is not None:
            dst_ip = ip.dst
            flow_key = self._flow_key(pkt, ip)
        elif arp_pkt is not None:
            # ARP request: dst_ip is the target host. ARP reply: dst_ip is
            # the requester. Either way, forward toward dst_ip, never flood.
            dst_ip = arp_pkt.dst_ip
            flow_key = (arp_pkt.src_ip, arp_pkt.dst_ip, "arp")
        else:
            return  # IPv6 / other noise: drop, never flood on a looped topology

        out_port = self._out_port(dpid, dst_ip, flow_key)
        if out_port is None:
            return

        actions = [parser.OFPActionOutput(out_port)]

        if ip is not None:
            match = self._ip_match(parser, pkt, ip)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 10, match, actions, msg.buffer_id, idle_timeout=30)
                return
            self.add_flow(datapath, 10, match, actions, idle_timeout=30)

        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        datapath.send_msg(parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port,
            actions=actions, data=data,
        ))

    # ------------------------------------------------------------------
    def _out_port(self, dpid, dst_ip, flow_key):
        """Static, direction-aware forwarding. ECMP happens only at the
        edge switches (s1, s4) when the destination is across the core."""
        dst_sw = HOST_SWITCH.get(dst_ip)
        if dst_sw is None:
            return None

        # Destination host is attached to this very switch.
        if dpid == dst_sw:
            return HOST_PORT.get(dpid)

        # Edge switch, destination is on the far side: ECMP hash picks a path.
        if dpid in EDGE_PORTS:
            src_node = DPID_TO_NODE[dpid]
            dst_node = DPID_TO_NODE[dst_sw]
            path = ecmp_select_path(self.graph, src_node, dst_node, flow_key)
            if path and len(path) > 1:
                return EDGE_PORTS[dpid].get(path[1])
            return None

        # Middle switch: send toward the edge switch that owns the destination.
        return MIDDLE_PORTS.get(dpid, {}).get(dst_sw)

    @staticmethod
    def _flow_key(pkt, ip):
        t = pkt.get_protocol(tcp.tcp)
        if t is not None:
            return (ip.src, ip.dst, ip.proto, t.src_port, t.dst_port)
        u = pkt.get_protocol(udp.udp)
        if u is not None:
            return (ip.src, ip.dst, ip.proto, u.src_port, u.dst_port)
        return (ip.src, ip.dst, ip.proto)

    @staticmethod
    def _ip_match(parser, pkt, ip):
        fields = dict(eth_type=ether_types.ETH_TYPE_IP,
                      ipv4_src=ip.src, ipv4_dst=ip.dst, ip_proto=ip.proto)
        t = pkt.get_protocol(tcp.tcp)
        u = pkt.get_protocol(udp.udp)
        if t is not None:
            fields.update(tcp_src=t.src_port, tcp_dst=t.dst_port)
        elif u is not None:
            fields.update(udp_src=u.src_port, udp_dst=u.dst_port)
        return parser.OFPMatch(**fields)
