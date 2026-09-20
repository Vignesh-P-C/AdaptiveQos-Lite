"""
controller/ecmp_fallback.py — standard ECMP path selection

Roadmap Tier 2 #3: ECMP splits traffic across equal-cost paths using a
hash of packet header fields. It's static and congestion-blind — it
picks a path once, from the 5-tuple, and never revisits that decision
regardless of live jitter/loss. That's precisely the gap AdaptiveQoS-
Lite's Stage 4 agent is meant to close for real-time flows; ECMP stays
exactly this simple on purpose, because best-effort flows are supposed
to be cheap to handle.

This module is intentionally topology-aware but controller-agnostic:
give it a graph and a (src, dst) pair, it returns the deterministic
path for that flow's 5-tuple. Both baselines/static_ecmp_only.py (the
"do nothing smart" comparison run) and, later, main_app.py's
best-effort branch import from here — one implementation, so the
"same ECMP" claim in the results comparison is actually true.
"""

import hashlib


def find_all_paths(graph, src, dst, path=None):
    """All simple paths src -> dst in an undirected adjacency-dict graph
    {node: set(neighbors)}. Small topologies (per Objective 1, 10-15
    switches) make brute-force enumeration fine — no need for k-shortest-
    path machinery here."""
    path = path or [src]
    if src == dst:
        return [path]
    if src not in graph:
        return []

    paths = []
    for neighbor in graph[src]:
        if neighbor not in path:
            for p in find_all_paths(graph, neighbor, dst, path + [neighbor]):
                paths.append(p)
    return paths


def equal_cost_paths(graph, src, dst, cost_fn=len):
    """All shortest (by cost_fn, default hop count) paths src -> dst."""
    all_paths = find_all_paths(graph, src, dst)
    if not all_paths:
        return []
    best_cost = min(cost_fn(p) for p in all_paths)
    return [p for p in all_paths if cost_fn(p) == best_cost]


def ecmp_select_path(graph, src, dst, flow_key, cost_fn=len):
    """Deterministically pick one of the equal-cost paths for this flow,
    by hashing the 5-tuple flow_key. Same flow_key always yields the
    same path (no per-packet reordering); different flows spread across
    the available equal-cost paths.

    flow_key: any hashable tuple identifying the flow, e.g.
        (src_ip, dst_ip, proto, src_port, dst_port)
    """
    paths = equal_cost_paths(graph, src, dst, cost_fn=cost_fn)
    if not paths:
        return None
    digest = hashlib.md5(repr(flow_key).encode()).hexdigest()
    index = int(digest, 16) % len(paths)
    return paths[index]


def graph_from_topo(links):
    """Build the {node: set(neighbors)} adjacency dict this module
    expects, from a plain list of (a, b) link tuples — e.g. read
    straight from topology/topo.py's link list, or hardcoded to match
    RedundantPathTopo for the baseline/agent to reason about without
    querying Mininet at runtime."""
    graph = {}
    for a, b in links:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)
    return graph


# Matches topology/topo.py's RedundantPathTopo(num_middle_switches=2)
# default topology. Keep in sync if you change that file's width.
DEFAULT_TOPO_LINKS = [
    ("h1", "s1"), ("h2", "s4"),
    ("s1", "s2"), ("s2", "s4"),
    ("s1", "s3"), ("s3", "s4"),
]
TOPO_GRAPH = graph_from_topo(DEFAULT_TOPO_LINKS)

NODE_TO_DPID = {"s1": 1, "s2": 2, "s3": 3, "s4": 4}
DPID_TO_NODE = {v: k for k, v in NODE_TO_DPID.items()}
HOST_SWITCH = {"10.0.0.1": 1, "10.0.0.2": 4}
HOST_PORT = {1: 1, 4: 1}
EDGE_PORTS = {1: {"s2": 2, "s3": 3}, 4: {"s2": 2, "s3": 3}}
MIDDLE_PORTS = {2: {1: 1, 4: 2}, 3: {1: 1, 4: 2}}


def make_flow_key(pkt, ip):
    from ryu.lib.packet import tcp, udp
    t = pkt.get_protocol(tcp.tcp)
    if t is not None:
        return (ip.src, ip.dst, ip.proto, t.src_port, t.dst_port)
    u = pkt.get_protocol(udp.udp)
    if u is not None:
        return (ip.src, ip.dst, ip.proto, u.src_port, u.dst_port)
    return (ip.src, ip.dst, ip.proto)


def ip_match(parser, pkt, ip):
    from ryu.lib.packet import tcp, udp, ether_types
    fields = dict(eth_type=ether_types.ETH_TYPE_IP,
                  ipv4_src=ip.src, ipv4_dst=ip.dst, ip_proto=ip.proto)
    t = pkt.get_protocol(tcp.tcp)
    u = pkt.get_protocol(udp.udp)
    if t is not None:
        fields.update(tcp_src=t.src_port, tcp_dst=t.dst_port)
    elif u is not None:
        fields.update(udp_src=u.src_port, udp_dst=u.dst_port)
    return parser.OFPMatch(**fields)


def out_port_for(dpid, dst_ip, flow_key, path=None):
    """Direction-aware forwarding port for this fixed topology; never
    floods. path=(edge, mid, edge) pins the middle-switch hop (used by
    the agent for real-time flows, both directions); omit it for
    hash-based ECMP (best-effort flows)."""
    dst_sw = HOST_SWITCH.get(dst_ip)
    if dst_sw is None:
        return None
    if dpid == dst_sw:
        return HOST_PORT.get(dpid)
    if dpid in EDGE_PORTS:
        if path is not None:
            # path[1] is the middle switch the agent picked; both edge
            # switches use the same middle-switch port mapping, so this
            # applies symmetrically to either traffic direction.
            next_hop = path[1]
        else:
            chosen = ecmp_select_path(TOPO_GRAPH, DPID_TO_NODE[dpid], DPID_TO_NODE[dst_sw], flow_key)
            if not chosen or len(chosen) < 2:
                return None
            next_hop = chosen[1]
        return EDGE_PORTS[dpid].get(next_hop)
    return MIDDLE_PORTS.get(dpid, {}).get(dst_sw)
