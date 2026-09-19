"""
controller/classifier.py — Stage 3: Traffic Classifier

Tags each flow as "real_time" or "best_effort" at flow setup, using
port-number and DSCP-marking heuristics (roadmap Tier 1 #2 flow-table
match fields; IDF §7.3). This is a heuristic classifier by design —
the point (per the novelty claim) is that classification stays cheap
so only real-time flows pay for the adaptive agent's attention later;
best-effort traffic is left on ECMP untouched.

Heuristics, in priority order:
  1. DSCP marking (IP ToS field) — EF (46) or AF4x (34-38) => real_time.
     This is the "correct" signal if the traffic source marks it.
  2. UDP + a port in REALTIME_UDP_PORTS (common VoIP/RTP ranges used
     by traffic_gen/generate_traffic.py to simulate video/VoIP).
  3. Everything else => best_effort (this is also every TCP bulk flow
     from generate_traffic.py's bulk mode).

Used from main_app.py's packet_in handler once Stage 4 wiring lands
(week 5-6); exposed standalone here so traffic_gen/generate_traffic.py
and tests can classify a flow description without needing a running
Ryu instance.
"""

# ryu is only needed by classify_packet(), not by the pure classify_flow()
# helper — import lazily so this module (and its tests) don't require a
# Ryu install just to exercise the classification logic itself.

# DSCP values (upper 6 bits of the IP ToS/TC byte) treated as real-time.
DSCP_REALTIME = {46, 34, 36, 38}  # EF, AF41, AF42, AF43

# UDP port range used by traffic_gen/generate_traffic.py for simulated
# VoIP/video (RTP-like) traffic. Keep in sync with that file.
REALTIME_UDP_PORTS = range(16384, 16394)

REAL_TIME = "real_time"
BEST_EFFORT = "best_effort"


def classify_flow(dscp=None, proto=None, src_port=None, dst_port=None):
    """Pure function version — classify from already-extracted fields.
    Used by tests and by traffic_gen without needing a Ryu packet."""
    if dscp is not None and dscp in DSCP_REALTIME:
        return REAL_TIME

    if proto == "udp":
        if (src_port in REALTIME_UDP_PORTS) or (dst_port in REALTIME_UDP_PORTS):
            return REAL_TIME

    return BEST_EFFORT


def classify_packet(pkt):
    """Classify directly from a ryu.lib.packet.Packet (as seen in a
    packet_in handler). Returns (REAL_TIME | BEST_EFFORT, flow_key) or
    (None, None) if the packet isn't classifiable IP traffic (e.g. ARP)."""
    from ryu.lib.packet import ethernet, ipv4, udp, tcp, ether_types

    eth = pkt.get_protocols(ethernet.ethernet)[0]
    if eth.ethertype != ether_types.ETH_TYPE_IP:
        return None, None

    ip = pkt.get_protocol(ipv4.ipv4)
    if ip is None:
        return None, None

    dscp = ip.tos >> 2  # ToS byte: top 6 bits = DSCP, bottom 2 = ECN

    udp_hdr = pkt.get_protocol(udp.udp)
    tcp_hdr = pkt.get_protocol(tcp.tcp)

    if udp_hdr is not None:
        proto, src_port, dst_port = "udp", udp_hdr.src_port, udp_hdr.dst_port
    elif tcp_hdr is not None:
        proto, src_port, dst_port = "tcp", tcp_hdr.src_port, tcp_hdr.dst_port
    else:
        proto, src_port, dst_port = None, None, None

    label = classify_flow(dscp=dscp, proto=proto, src_port=src_port, dst_port=dst_port)
    flow_key = (ip.src, ip.dst, proto, src_port, dst_port)
    return label, flow_key
