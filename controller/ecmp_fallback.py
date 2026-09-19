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
