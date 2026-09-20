"""
experiments/run_experiment.py -- end-to-end demo: static baseline vs adaptive agent

No Ryu needed. Mininet builds the repo's topology (topology/topo.py); the
switches run with NO controller, and this script installs the forwarding
rules itself with ovs-ofctl, so it can steer the h1<->h2 flow onto either
the s2 path or the s3 path.

Scenario (STEPS steps, ~1.5 s each):
  * steps 0..DEGRADE_AT-1 : both paths healthy (s2 path is faster)
  * at step DEGRADE_AT    : the s2->s4 link gets +JITTER of netem jitter
  * static_ecmp : always uses the s1-s2-s4 path (what a static policy
                  tuned for the faster path does; it never re-decides)
  * adaptive    : EpsilonGreedyAgent picks a path every step from the
                  measured jitter / latency / loss of the previous choices
Each step measures the chosen path with PING_COUNT pings. Jitter here is
the RTT standard deviation (ping "mdev") -- a proxy, not RFC 3550 jitter.

Run from the repo root, Ryu NOT running:
    sudo mn -c
    sudo python3 experiments/run_experiment.py
Output: a table on screen + CSVs in data/raw/ (Log A per method, Log B for the agent).
"""

import os
import re
import sys
import time
from functools import partial

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel

from topology.topo import RedundantPathTopo
from controller.agent import EpsilonGreedyAgent
from evaluation.logger import FlowMetricsLogger, AgentInternalLogger
# This OVS only speaks OpenFlow 1.3; make every dpctl() call use it.
OVSSwitch.dpctl = lambda self, *args: self.cmd("ovs-ofctl", "-O", "OpenFlow13", args[0], self.name, *args[1:])

STEPS = 40
DEGRADE_AT = 20
PING_COUNT = 20
PING_INTERVAL = 0.05
JITTER = "20ms"
PATH_A = ("s1", "s2", "s4")
PATH_B = ("s1", "s3", "s4")
STATIC_PATH = PATH_A
DEGRADE_MID = "s2"
SCENARIO = "degrade_s2"


# ---------------------------------------------------------------- helpers
def port_toward(net, a, b):
    """OpenFlow port number on node `a` that faces node `b`."""
    sw, other = net.get(a), net.get(b)
    for link in net.linksBetween(sw, other):
        intf = link.intf1 if link.intf1.node == sw else link.intf2
        return sw.ports[intf]
    raise RuntimeError("no link between %s and %s" % (a, b))


def install_static_rules(net):
    """Rules that never change: delivery to hosts and the middle switches."""
    ip1, ip2 = net.get("h1").IP(), net.get("h2").IP()
    net.get("s1").dpctl("add-flow", "priority=100,ip,nw_dst=%s,actions=output:%d"
                        % (ip1, port_toward(net, "s1", "h1")))
    net.get("s4").dpctl("add-flow", "priority=100,ip,nw_dst=%s,actions=output:%d"
                        % (ip2, port_toward(net, "s4", "h2")))
    for mid in ("s2", "s3"):
        sw = net.get(mid)
        sw.dpctl("add-flow", "priority=100,ip,nw_dst=%s,actions=output:%d"
                 % (ip2, port_toward(net, mid, "s4")))
        sw.dpctl("add-flow", "priority=100,ip,nw_dst=%s,actions=output:%d"
                 % (ip1, port_toward(net, mid, "s1")))


def steer(net, mid):
    """Send h1->h2 (at s1) and h2->h1 (at s4) through middle switch `mid`."""
    ip1, ip2 = net.get("h1").IP(), net.get("h2").IP()
    net.get("s1").dpctl("add-flow", "priority=200,ip,nw_dst=%s,actions=output:%d"
                        % (ip2, port_toward(net, "s1", mid)))
    net.get("s4").dpctl("add-flow", "priority=200,ip,nw_dst=%s,actions=output:%d"
                        % (ip1, port_toward(net, "s4", mid)))


def set_jitter(net, on):
    """Add / remove netem jitter on the DEGRADE_MID -> s4 link."""
    mid, s4 = net.get(DEGRADE_MID), net.get("s4")
    for link in net.linksBetween(mid, s4):
        intf = link.intf1 if link.intf1.node == mid else link.intf2
        p = getattr(intf, "params", None) or {}
        try:
            intf.config(bw=p.get("bw"), delay=p.get("delay"),
                        jitter=(JITTER if on else None))
        except TypeError:
            raise SystemExit("This Mininet version does not support jitter= "
                             "(run `mn --version`; need 2.3.0 or newer).")


def measure(h1, dst_ip):
    """Ping the destination; return (latency_ms, jitter_ms, loss_pct)."""
    out = h1.cmd("ping -c %d -i %s -W 1 -q %s" % (PING_COUNT, PING_INTERVAL, dst_ip))
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    loss_pct = float(loss.group(1)) if loss else 100.0
    if rtt:
        return float(rtt.group(2)), float(rtt.group(4)), loss_pct
    return 1000.0, 1000.0, loss_pct


# ------------------------------------------------------------ experiment
def run_method(net, method, flow_log, agent=None):
    h1, ip2 = net.get("h1"), net.get("h2").IP()
    set_jitter(net, False)
    rows = []
    for step in range(STEPS):
        if step == DEGRADE_AT:
            set_jitter(net, True)
        if agent is not None:
            path = agent.select_path([PATH_A, PATH_B], flow_key="h1-h2-icmp")
        else:
            path = STATIC_PATH
        steer(net, path[1])
        latency, jitter, loss = measure(h1, ip2)
        if agent is not None:
            agent.update(path, jitter, latency, loss / 100.0)
        flow_log.write_row("h1-h2-icmp", "realtime", "-".join(path),
                           latency, jitter, loss, 0)
        rows.append(dict(step=step, path=path, latency=latency, jitter=jitter, loss=loss))
        print("  %-11s step %2d  %-9s latency=%6.1f ms  jitter=%6.2f ms  loss=%4.1f%%%s"
              % (method, step, "-".join(path), latency, jitter, loss,
                 "   <-- link degraded" if step == DEGRADE_AT else ""))
    set_jitter(net, False)
    return rows


def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def steps_to_adapt(rows):
    picks = [r["path"] for r in rows]
    for i in range(DEGRADE_AT, len(picks) - 2):
        if picks[i:i + 3] == [PATH_B] * 3:
            return i - DEGRADE_AT
    return None


def summarize(results):
    print("\n" + "=" * 78)
    print("RESULTS  (link on the s2 path degrades at step %d of %d)" % (DEGRADE_AT, STEPS))
    print("=" * 78)
    print("%-13s %14s %14s %14s %14s" %
          ("method", "jitter before", "jitter after", "latency before", "latency after"))
    stats = {}
    for m, rows in results.items():
        pre = [r for r in rows if r["step"] < DEGRADE_AT]
        post = [r for r in rows if r["step"] >= DEGRADE_AT]
        stats[m] = dict(jpre=avg([r["jitter"] for r in pre]),
                        jpost=avg([r["jitter"] for r in post]),
                        lpre=avg([r["latency"] for r in pre]),
                        lpost=avg([r["latency"] for r in post]))
        s = stats[m]
        print("%-13s %11.2f ms %11.2f ms %11.1f ms %11.1f ms"
              % (m, s["jpre"], s["jpost"], s["lpre"], s["lpost"]))
    sp, ap = stats["static_ecmp"]["jpost"], stats["adaptive"]["jpost"]
    if sp > 0:
        print("\nJitter after degradation: adaptive is %.1f%% lower than static"
              % (100.0 * (sp - ap) / sp))
    n = steps_to_adapt(results["adaptive"])
    print("Time to adapt: %s"
          % ("%d steps (~%.0f s) after the link degraded" % (n, n * 1.5)
             if n is not None else "agent did not settle on the healthy path"))
    print("Note: jitter = RTT std-dev from ping (proxy); one scenario, emulated network.")


def main():
    setLogLevel("warning")
    net = Mininet(topo=RedundantPathTopo(),
                  switch=partial(OVSSwitch, failMode="secure"),
                  controller=None, link=TCLink)
    net.start()
    try:
        net.staticArp()
        install_static_rules(net)
        time.sleep(1)
        steer(net, "s2")
        latency, jitter, loss = measure(net.get("h1"), net.get("h2").IP())
        print("Sanity check h1->h2: latency=%.1f ms, loss=%.0f%%" % (latency, loss))
        if loss >= 100:
            print("No connectivity. Is Ryu still running or did `sudo mn -c` get skipped?")
            return

        stamp = time.strftime("%Y%m%d-%H%M%S")
        results = {}

        print("\n--- static_ecmp ---")
        flow_log = FlowMetricsLogger(method="static_ecmp", scenario=SCENARIO,
                                     run_timestamp=stamp)
        results["static_ecmp"] = run_method(net, "static_ecmp", flow_log)
        flow_log.close()

        print("\n--- adaptive ---")
        agent_log = AgentInternalLogger()
        agent = EpsilonGreedyAgent(seed=int(stamp[-2:]), logger=agent_log, w_latency=0.05)
        flow_log = FlowMetricsLogger(method="adaptive", scenario=SCENARIO,
                                     run_timestamp=stamp)
        results["adaptive"] = run_method(net, "adaptive", flow_log, agent)
        flow_log.close()
        agent_log.close()

        summarize(results)
    finally:
        net.stop()


if __name__ == "__main__":
    main()
