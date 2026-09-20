#!/usr/bin/env bash
# evaluation/run_all.sh -- sweep every (method, scenario) pair.
# Starts ryu-manager for each method, drives all three scenarios against
# it, then moves to the next method. Run from the repo root:
#   sudo ./evaluation/run_all.sh
set -e
cd "$(dirname "$0")/.."
DURATION=${DURATION:-60}

declare -A APPS=( [ecmp]="baselines/static_ecmp_only.py" [adaptiveqos]="controller/main_app.py" )

for method in ecmp adaptiveqos; do
  for scenario in light moderate heavy; do
    echo "=== $method / $scenario ==="
    mn -c >/dev/null 2>&1 || true
    ADAPTIVEQOS_SCENARIO=$scenario ryu-manager "${APPS[$method]}" &
    ryu_pid=$!
    sleep 3
    ADAPTIVEQOS_STP=0 python3 evaluation/run_scenarios.py "$scenario" --method "$method" --duration "$DURATION"
    kill "$ryu_pid" 2>/dev/null || true
    wait "$ryu_pid" 2>/dev/null || true
    sleep 2
  done
done

echo "All scenarios done. Run: python3 evaluation/analyze_results.py"
