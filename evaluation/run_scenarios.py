"""evaluation/run_scenarios.py -- drive one traffic scenario against a
running Ryu controller and log the result to Log A (via telemetry.py,
already wired into both controller/main_app.py and
baselines/static_ecmp_only.py).

Two terminals, same convention as topology/topo.py:
    # terminal 1 -- pick the method under test
    ADAPTIVEQOS_SCENARIO=moderate ryu-manager controller/main_app.py
    # terminal 2
    sudo python3 evaluation/run_scenarios.py moderate

Run evaluation/run_all.sh to sweep every (method, scenario) pair.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mininet.log import setLogLevel

from topology.topo import build_net
from traffic_gen.generate_traffic import run_scenario, SCENARIOS

CONNECT_WAIT_SEC = 5


def main():
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=list(SCENARIOS))
    p.add_argument("--duration", type=int, default=60)
    args = p.parse_args()

    setLogLevel("warning")
    net = build_net(num_middle_switches=2, use_remote_controller=True)
    net.start()
    try:
        print("Waiting %ds for switches to connect to the controller..." % CONNECT_WAIT_SEC)
        time.sleep(CONNECT_WAIT_SEC)

        loss = net.pingAll(timeout="2")
        if loss >= 100:
            print("No connectivity -- is the Ryu controller running?")
            return 1

        print("Running scenario '%s' for %ds" % (args.scenario, args.duration))
        run_scenario(net, args.scenario, duration_sec=args.duration)
        time.sleep(args.duration + 2)

        net.get("h1").cmd("pkill -9 -f iperf3 2>/dev/null")
        net.get("h2").cmd("pkill -9 -f iperf3 2>/dev/null")
        print("Scenario done. Check data/raw/ for the logged CSVs.")
    finally:
        net.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
