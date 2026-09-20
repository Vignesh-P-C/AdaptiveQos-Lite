"""controller/main_app.py -- AdaptiveQoS-Lite Ryu app: full pipeline.

Real-time flows -> agent picks path, best-effort -> ECMP hash. Both go
through ecmp_fallback.out_port_for so port lookup is one code path.
Reward feedback for real-time flows comes from telemetry's per-switch
jitter, polled once a second for each path currently in use.

Run:
    ryu-manager controller/main_app.py
    sudo python3 topology/topo.py
"""

import os

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, arp, ether_types
from ryu.lib import hub

from telemetry import TelemetryCollector  # also puts repo root on sys.path
from classifier import classify_packet, REAL_TIME
from agent import EpsilonGreedyAgent
from ecmp_fallback import out_port_for, make_flow_key, ip_match, NODE_TO_DPID
from evaluation.logger import AgentInternalLogger, SetupCostLogger

REWARD_POLL_SEC = 1.0
CANDIDATE_PATHS = [("s1", "s2", "s4"), ("s1", "s3", "s4")]


class AdaptiveQoSLiteApp(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self._reported_ready = False

        scenario = os.environ.get("ADAPTIVEQOS_SCENARIO", "unlabeled")
        self.telemetry = TelemetryCollector(self, method="adaptiveqos", scenario=scenario)
        hub.spawn(self.telemetry.run)

        self.agent = EpsilonGreedyAgent(logger=AgentInternalLogger(scenario=scenario))
        self.rt_paths = {}  # flow_key -> path currently assigned by the agent
        hub.spawn(self._reward_loop)

        self.setup_cost = SetupCostLogger()
        self.setup_cost.start("adaptiveqos")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        self.datapaths[datapath.id] = datapath
        self.logger.info("Switch connected: dpid=%s", datapath.id)
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

        if not self._reported_ready and len(self.datapaths) >= len(NODE_TO_DPID):
            self.setup_cost.finish("adaptiveqos", gpu_used="no")
            self._reported_ready = True

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        self.telemetry.handle_port_stats_reply(ev)

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def echo_reply_handler(self, ev):
        self.telemetry.handle_echo_reply(ev)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle_timeout=0):
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id, priority=priority,
                                     match=match, instructions=inst, idle_timeout=idle_timeout)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match,
                                     instructions=inst, idle_timeout=idle_timeout)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
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
            flow_key = make_flow_key(pkt, ip)
            label, _ = classify_packet(pkt)
        elif arp_pkt is not None:
            dst_ip = arp_pkt.dst_ip
            flow_key = (arp_pkt.src_ip, arp_pkt.dst_ip, "arp")
            label = None
        else:
            return

        path = self._route_realtime(flow_key) if label == REAL_TIME else None
        out_port = out_port_for(dpid, dst_ip, flow_key, path=path)
        if out_port is None:
            return

        actions = [parser.OFPActionOutput(out_port)]
        if ip is not None:
            match = ip_match(parser, pkt, ip)
            self.add_flow(datapath, 10, match, actions, msg.buffer_id, idle_timeout=10)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                return

        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        datapath.send_msg(parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port,
            actions=actions, data=data,
        ))

    def _route_realtime(self, flow_key):
        path = self.agent.select_path(CANDIDATE_PATHS, flow_key=flow_key)
        self.rt_paths[flow_key] = path
        return path

    def _reward_loop(self):
        """Once a second, score every path currently assigned to a
        real-time flow using the middle switch's telemetry jitter."""
        while True:
            hub.sleep(REWARD_POLL_SEC)
            for path in set(self.rt_paths.values()):
                mid_dpid = NODE_TO_DPID.get(path[1])
                if mid_dpid is None:
                    continue
                latency, jitter = self.telemetry._current_latency_jitter(mid_dpid)
                self.agent.update(path, jitter, latency)
