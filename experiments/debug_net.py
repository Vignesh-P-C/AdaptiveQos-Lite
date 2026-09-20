"""
experiments/debug_net.py -- builds the same network as run_experiment.py,
installs the same rules, prints what the switches actually hold, then
opens the Mininet CLI so you can poke around.
Run from the repo root:  sudo mn -c && sudo python3 experiments/debug_net.py
"""
import os
import sys
from functools import partial

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI

from topology.topo import RedundantPathTopo
from experiments.run_experiment import install_static_rules, steer

net = Mininet(topo=RedundantPathTopo(),
              switch=partial(OVSSwitch, failMode="secure"),
              controller=None, link=TCLink)
net.start()
net.staticArp()

print("\n=== PORTS (interface -> port number) ===")
for n in ("s1", "s2", "s3", "s4"):
    print(n, sorted((i.name, p) for i, p in net.get(n).ports.items()))

install_static_rules(net)
steer(net, "s2")

print("\n=== FLOWS INSTALLED ===")
for n in ("s1", "s2", "s3", "s4"):
    print("--", n)
    print(net.get(n).dpctl("dump-flows"))

print("=== h1 ARP TABLE ===")
print(net.get("h1").cmd("arp -n"))
print("=== h1 -> h2 PING ===")
print(net.get("h1").cmd("ping -c 3 -W 1 %s" % net.get("h2").IP()))

CLI(net)
net.stop()
