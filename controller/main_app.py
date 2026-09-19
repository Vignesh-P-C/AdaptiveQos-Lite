"""
controller/main_app.py — AdaptiveQoS-Lite Ryu application (entrypoint)

Week 1-2: this is deliberately just a learning switch. Roadmap Tier 1 #4
is explicit that packet_in + basic learning-switch pattern comes BEFORE
anything adaptive — get the OpenFlow handshake and flow install path
solid first, prove it with pingall, then layer telemetry/classifier/
agent on top in later weeks without having to debug two things at once.

Week 3-4: telemetry.py is wired in here (hub.spawn background poller).
Stage 3 classifier and Stage 4 agent hook in via the TODOs marked below
— that's week 5-6 scope per the timeline, not built yet on purpose.

Run:
    ryu-manager controller/main_app.py
then, in another terminal:
    sudo python3 topology/topo.py
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib import hub

from telemetry import TelemetryCollector

# Uncomment once Stage 3 (week 5-6) lands:
# from classifier import FlowClassifier


class AdaptiveQoSLiteApp(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # MAC learning table: {dpid: {mac: port}}
        self.mac_to_port = {}
        # Datapaths currently connected, keyed by dpid — telemetry.py
        # polls stats from these.
        self.datapaths = {}

        self.telemetry = TelemetryCollector(self)
        self.telemetry_thread = hub.spawn(self.telemetry.run)

        # TODO (week 5-6): self.classifier = FlowClassifier()
        # TODO (week 5-6): self.agent = AdaptiveRoutingAgent()

    # ------------------------------------------------------------------
    # OpenFlow handshake
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """features-request/features-reply handshake. Also installs the
        table-miss flow entry (send unmatched packets to the controller)."""
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        self.datapaths[datapath.id] = datapath
        self.logger.info(
            "Switch connected: dpid=%s (OpenFlow handshake complete)",
            datapath.id,
        )

        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)
        ]
        self.add_flow(datapath, priority=0, match=match, actions=actions)

    @set_ev_cls(ofp_event.EventOFPStateChange, MAIN_DISPATCHER)
    def state_change_handler(self, ev):
        pass  # datapaths dict already updated in switch_features_handler

    # ------------------------------------------------------------------
    # Telemetry (Stage 2, week 3-4) — pure read path, installs no flows
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        self.telemetry.handle_port_stats_reply(ev)

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def echo_reply_handler(self, ev):
        self.telemetry.handle_echo_reply(ev)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None,
                 idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(
                datapath=datapath, buffer_id=buffer_id, priority=priority,
                match=match, instructions=inst,
                idle_timeout=idle_timeout, hard_timeout=hard_timeout,
            )
        else:
            mod = parser.OFPFlowMod(
                datapath=datapath, priority=priority, match=match,
                instructions=inst,
                idle_timeout=idle_timeout, hard_timeout=hard_timeout,
            )
        datapath.send_msg(mod)

    # ------------------------------------------------------------------
    # packet_in — basic learning-switch pattern
    # ------------------------------------------------------------------
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
            return  # ignore LLDP for now (used later for topology discovery)

        dst = eth.dst
        src = eth.src

        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        # TODO (week 5-6): before deciding out_port, ask the classifier
        # whether this flow is real-time or best-effort. Real-time flows
        # get routed by the agent (lowest-jitter path); best-effort
        # flows fall through to ecmp_fallback.py. For now: plain L2
        # learning switch, so the week-1-2 milestone (pingall) works.
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions, msg.buffer_id, idle_timeout=30)
                return
            else:
                self.add_flow(datapath, 1, match, actions, idle_timeout=30)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port,
            actions=actions, data=data,
        )
        datapath.send_msg(out)
