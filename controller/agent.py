"""
controller/agent.py -- AdaptiveQoS-Lite Stage 4: lightweight online-learning agent

An epsilon-greedy multi-armed bandit. Each candidate path between two
switches is one "arm". The reward for an arm is the negative of a weighted
sum of jitter, latency and loss measured on that path, so the agent learns
to prefer the path with the lowest jitter.

Design notes
- Pure Python, no Ryu/Mininet imports, so it can be unit-tested on its own.
- Constant step size (alpha) => old observations are forgotten, so the agent
  can react when a link degrades partway through a run.
- epsilon decays but never drops below eps_min, so a path that recovers
  can be rediscovered.
- Cold start: every arm is tried once before estimates are trusted.

Usage
    agent = EpsilonGreedyAgent(log_path="data/raw/agent_decisions.csv")
    path = agent.select_path([("s1", "s2", "s4"), ("s1", "s3", "s4")])
    ... measure jitter/latency/loss on that path ...
    agent.update(path, jitter_ms, latency_ms, loss_fraction)
"""

import csv
import os
import random
import time


class EpsilonGreedyAgent:
    def __init__(self,
                 alpha=0.2,
                 eps_start=0.3,
                 eps_min=0.05,
                 eps_decay=0.99,
                 w_jitter=1.0,
                 w_latency=0.2,
                 w_loss=100.0,
                 seed=None,
                 log_path=None):
        self.alpha = alpha
        self.eps = eps_start
        self.eps_min = eps_min
        self.eps_decay = eps_decay
        self.w_jitter = w_jitter
        self.w_latency = w_latency
        self.w_loss = w_loss

        self.q = {}   # path -> estimated reward (higher is better)
        self.n = {}   # path -> number of updates received
        self.rng = random.Random(seed)

        self._log_file = None
        self._writer = None
        if log_path:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            self._log_file = open(log_path, "w", newline="")
            self._writer = csv.writer(self._log_file)
            self._writer.writerow(
                ["time", "event", "flow", "path", "mode", "reward", "epsilon", "q_values"])

    # ------------------------------------------------------------------
    def reward(self, jitter_ms, latency_ms=0.0, loss=0.0):
        """Higher is better. loss is a fraction in [0, 1]."""
        return -(self.w_jitter * jitter_ms
                 + self.w_latency * latency_ms
                 + self.w_loss * loss)

    def select_path(self, candidate_paths, flow_key=None):
        candidates = [tuple(p) for p in candidate_paths]
        untried = [p for p in candidates if self.n.get(p, 0) == 0]

        if untried:
            path, mode = self.rng.choice(untried), "cold_start"
        elif self.rng.random() < self.eps:
            path, mode = self.rng.choice(candidates), "explore"
        else:
            path, mode = max(candidates, key=lambda p: self.q[p]), "exploit"

        self.eps = max(self.eps_min, self.eps * self.eps_decay)
        self._log("select", flow_key, path, mode, None)
        return path

    def update(self, path, jitter_ms, latency_ms=0.0, loss=0.0):
        path = tuple(path)
        r = self.reward(jitter_ms, latency_ms, loss)
        if path not in self.q:
            self.q[path] = r
        else:
            self.q[path] += self.alpha * (r - self.q[path])
        self.n[path] = self.n.get(path, 0) + 1
        self._log("update", None, path, None, r)

    def best_path(self):
        return max(self.q, key=self.q.get) if self.q else None

    # ------------------------------------------------------------------
    def _log(self, event, flow, path, mode, reward):
        if not self._writer:
            return
        qs = ";".join("%s=%.2f" % ("-".join(p), v) for p, v in self.q.items())
        self._writer.writerow([
            "%.4f" % time.time(), event, flow, "-".join(path), mode,
            "" if reward is None else "%.3f" % reward,
            "%.3f" % self.eps, qs,
        ])
        self._log_file.flush()

    def close(self):
        if self._log_file:
            self._log_file.close()
            self._log_file = None
