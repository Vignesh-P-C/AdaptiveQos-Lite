const POLL_MS = 2000;

const chart = new Chart(document.getElementById("timeseries-chart"), {
  type: "line",
  data: {
    labels: [],
    datasets: [
      { label: "Latency (ms)", data: [], borderColor: "#4f8cff", tension: 0.25, pointRadius: 0 },
      { label: "Jitter (ms)", data: [], borderColor: "#3ddc84", tension: 0.25, pointRadius: 0 },
    ],
  },
  options: {
    responsive: true,
    animation: false,
    scales: {
      x: { ticks: { color: "#8b93a7" }, grid: { color: "#2a3350" } },
      y: { ticks: { color: "#8b93a7" }, grid: { color: "#2a3350" }, beginAtZero: true },
    },
    plugins: { legend: { labels: { color: "#e5e9f0" } } },
  },
});

function setPill(id, ok) {
  const el = document.getElementById(id);
  el.textContent = ok ? "Connected" : "Disconnected";
  el.className = ok ? "ok" : "bad";
}

async function fetchJSON(url) {
  const res = await fetch(url);
  return res.json();
}

function formatAgentStatus(secondsSince) {
  if (secondsSince == null) return { text: "no decisions yet", ok: false };
  if (secondsSince < 1) return { text: "last decision just now", ok: true };
  return { text: `last decision ${secondsSince.toFixed(0)}s ago`, ok: secondsSince < 15 };
}

async function refreshCurrentFlow() {
  const f = await fetchJSON("/api/current_flow");
  document.getElementById("flow-type").textContent = f.flow_type ?? "--";
  document.getElementById("flow-latency").textContent = f.latency_ms != null ? f.latency_ms.toFixed(2) + " ms" : "-- ms";
  document.getElementById("flow-jitter").textContent = f.jitter_ms != null ? f.jitter_ms.toFixed(2) + " ms" : "-- ms";
  document.getElementById("flow-loss").textContent = f.packet_loss_pct != null ? f.packet_loss_pct.toFixed(1) + " %" : "-- %";
}

async function refreshPaths(currentPath) {
  const paths = await fetchJSON("/api/paths");
  const grid = document.getElementById("paths-grid");
  grid.innerHTML = "";
  for (const p of paths) {
    const div = document.createElement("div");
    div.className = "path-card" + (p.path === currentPath ? " current" : "");
    div.innerHTML = `
      <h3>${p.path}</h3>
      <div class="row"><span>Latency</span><span>${p.latency_ms != null ? p.latency_ms.toFixed(2) + " ms" : "n/a"}</span></div>
      <div class="row"><span>Jitter</span><span>${p.jitter_ms != null ? p.jitter_ms.toFixed(2) + " ms" : "n/a"}</span></div>
      <div class="row"><span>Q-value</span><span>${p.q_value != null ? p.q_value.toFixed(2) : "untried"}</span></div>
      <div class="row"><span>Visits</span><span>${p.visits}</span></div>
    `;
    grid.appendChild(div);
  }
}

async function refreshDecisions() {
  const rows = await fetchJSON("/api/decisions");
  const tbody = document.querySelector("#decisions-table tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    const t = new Date(r.timestamp * 1000).toLocaleTimeString();
    tr.innerHTML = `<td>${t}</td><td>${r.flow_id || ""}</td><td>${r.chosen_path || ""}</td><td>${r.reward.toFixed(2)}</td>`;
    tbody.appendChild(tr);
  }
}

async function refreshTimeseries() {
  const ts = await fetchJSON("/api/timeseries");
  chart.data.labels = ts.timestamps.map(t => new Date(t * 1000).toLocaleTimeString());
  chart.data.datasets[0].data = ts.latency_ms;
  chart.data.datasets[1].data = ts.jitter_ms;
  chart.update();
}

async function refreshAll() {
  try {
    const s = await fetchJSON("/api/status");
    setPill("controller-status", s.controller_connected);
    const agentEl = document.getElementById("agent-status");
    const agentInfo = formatAgentStatus(s.seconds_since_last_decision);
    agentEl.textContent = agentInfo.text;
    agentEl.className = agentInfo.ok ? "ok" : "bad";
    document.getElementById("active-flows").textContent = s.active_flows ?? "--";
    document.getElementById("current-path").textContent = s.current_path ?? "--";
    document.getElementById("scenario").textContent = s.scenario ?? "--";
    document.getElementById("method").textContent = s.method ?? "--";

    await Promise.all([
      refreshCurrentFlow(),
      refreshPaths(s.current_path),
      refreshDecisions(),
      refreshTimeseries(),
    ]);
  } catch (e) {
    console.error("dashboard refresh failed", e);
  }
}

refreshAll();
setInterval(refreshAll, POLL_MS);
