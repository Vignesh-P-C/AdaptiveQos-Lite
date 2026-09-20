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
- Logging: pass an evaluation.logger.AgentInternalLogger (Log B) as
  `logger`. One row is written per routing decision, once its reward
  arrives via update(). With logger=None nothing is written.

Usage
    from evaluation.logger import AgentInternalLogger
    agent = EpsilonGreedyAgent(logger=AgentInternalLogger())
    path = agent.select_path([("s1", "s2", "s4"), ("s1", "s3", "s4")],
                             flow_key="h1-h2-udp-5004")
    ... measure jitter/latency/loss on that path ...
    agent.update(path, jitter_ms, latency_ms, loss_fraction)
"""

import random


class EpsilonGreedyAgent:
    def __init__(self,
                 alpha=0.2,
                 eps_start=0.3,
                 eps_min=0.05,
                 eps_decay=0.99,
                 w_jitter=1.0,
                 w_latency=0.05,
                 w_loss=100.0,
                 reward_floor=-20.0,
                 seed=None,
                 logger=None):
        self.alpha = alpha
        self.eps = eps_start
        self.eps_min = eps_min
        self.eps_decay = eps_decay
        self.w_jitter = w_jitter
        self.w_latency = w_latency
        self.w_loss = w_loss
        self.reward_floor = reward_floor

        self.q = {}   # path -> estimated reward (higher is better)
        self.n = {}   # path -> number of updates received
        self.rng = random.Random(seed)

        self.logger = logger      # AgentInternalLogger (Log B) or None
        self._pending = {}        # path -> [(flow_key, candidates), ...] awaiting reward

    # ------------------------------------------------------------------
    def reward(self, jitter_ms, latency_ms=0.0, loss=0.0):
        """Higher is better, floored so one outlier reading (e.g. a
        flow-install transient) can't poison a path's Q-estimate."""
        r = -(self.w_jitter * jitter_ms
              + self.w_latency * latency_ms
              + self.w_loss * loss)
        return max(r, self.reward_floor)

    def select_path(self, candidate_paths, flow_key=None):
        candidates = [tuple(p) for p in candidate_paths]
        untried = [p for p in candidates if self.n.get(p, 0) == 0]

        if untried:
            path = self.rng.choice(untried)
        elif self.rng.random() < self.eps:
            path = self.rng.choice(candidates)
        else:
            path = max(candidates, key=lambda p: self.q[p])

        self.eps = max(self.eps_min, self.eps * self.eps_decay)
        self._pending.setdefault(path, []).append((flow_key, candidates))
        return path

    def update(self, path, jitter_ms, latency_ms=0.0, loss=0.0):
        path = tuple(path)
        r = self.reward(jitter_ms, latency_ms, loss)
        if path not in self.q:
            self.q[path] = r
        else:
            self.q[path] += self.alpha * (r - self.q[path])
        self.n[path] = self.n.get(path, 0) + 1

        # One Log B row per decision, written when its reward arrives.
        waiting = self._pending.get(path)
        if self.logger is not None and waiting:
            flow_key, candidates = waiting.pop(0)
            self.logger.write_row(
                flow_id=flow_key if flow_key is not None else "",
                candidate_paths="|".join("-".join(p) for p in candidates),
                chosen_path="-".join(path),
                reward=round(r, 3),
                arm_estimates=self._fmt_estimates(),
            )

    def best_path(self):
        return max(self.q, key=self.q.get) if self.q else None

    def _fmt_estimates(self):
        return ";".join("%s=%.2f" % ("-".join(p), v) for p, v in self.q.items())
