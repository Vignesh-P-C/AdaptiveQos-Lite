"""
tests/sim_agent.py -- test the bandit agent against a FAKE network.

No Mininet/Ryu needed. Two paths with noisy jitter; path A starts good,
then degrades at step DEGRADE_AT. We check that the agent
  1) learns to prefer the good path, and
  2) switches to the other path after the degradation (time-to-adapt).

Run from the repo root:
    PYTHONPATH=. python3 tests/sim_agent.py
"""

import random
import statistics

from controller.agent import EpsilonGreedyAgent

PATH_A = ("s1", "s2", "s4")
PATH_B = ("s1", "s3", "s4")
STEPS = 1000
DEGRADE_AT = 500
WINDOW = 20          # "adapted" = last WINDOW picks were >= 90% on the new best path


def sample_metrics(path, step, rng):
    """Return (jitter_ms, latency_ms, loss) for one probe on `path`."""
    if path == PATH_A:
        base = 2.0 if step < DEGRADE_AT else 15.0   # A degrades halfway
    else:
        base = 5.0
    jitter = max(0.0, rng.gauss(base, base * 0.3))
    latency = 10.0 + 2.0 * jitter
    loss = 0.0
    return jitter, latency, loss


def run_once(seed):
    rng = random.Random(seed)
    agent = EpsilonGreedyAgent(seed=seed)
    picks = []
    adapted_at = None

    for step in range(STEPS):
        path = agent.select_path([PATH_A, PATH_B])
        agent.update(path, *sample_metrics(path, step, rng))
        picks.append(path)

        if step >= DEGRADE_AT and adapted_at is None and len(picks) >= WINDOW:
            recent = picks[-WINDOW:]
            if sum(p == PATH_B for p in recent) / WINDOW >= 0.9:
                adapted_at = step - DEGRADE_AT

    before = picks[100:DEGRADE_AT]           # skip the initial learning phase
    frac_good_before = sum(p == PATH_A for p in before) / len(before)
    return frac_good_before, adapted_at


if __name__ == "__main__":
    results = [run_once(s) for s in range(20)]
    fracs = [r[0] for r in results]
    adapt = [r[1] for r in results if r[1] is not None]

    print("Runs: %d, steps per run: %d, path A degrades at step %d"
          % (len(results), STEPS, DEGRADE_AT))
    print("Time on best path before degradation: %.1f%%"
          % (100 * statistics.mean(fracs)))
    if adapt:
        print("Steps to adapt after degradation: mean %.1f, max %d (%d/%d runs adapted)"
              % (statistics.mean(adapt), max(adapt), len(adapt), len(results)))
    else:
        print("Agent never adapted -- check alpha / epsilon settings")
