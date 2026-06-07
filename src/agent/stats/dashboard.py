"""Localhost stats dashboard for the email agent.

Reads from the SQLite stats DB (db.py) and streams live `email_processed`
events from the in-process events bus (events.py). When the agent graph
runs inside the same `langgraph dev` process, the dashboard can be mounted
alongside it and will get cards in real time. When run as a standalone
uvicorn (the `make dashboard` target), the live feed only fires while the
two processes share memory — typical setup runs the dashboard in the same
process as webapp.py.

Run standalone: uv run uvicorn agent.dashboard:app --port 8765
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from agent.stats.db import (
    get_category_breakdown,
    get_daily_counts,
    get_recent_events,
    get_top_senders,
    get_totals,
    init_db,
)
from agent.stats.events import attach_loop, subscribe

logger = logging.getLogger(__name__)

app = FastAPI(title="Email Agent — Stats")


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    attach_loop(asyncio.get_running_loop())


@app.get("/api/stats")
async def api_stats() -> JSONResponse:
    return JSONResponse(
        {
            "totals": get_totals(),
            "by_category": get_category_breakdown(),
            "daily": get_daily_counts(),
            "top_senders": get_top_senders(),
        }
    )


@app.get("/api/events")
async def api_events(limit: int = 50) -> JSONResponse:
    return JSONResponse({"events": get_recent_events(limit=limit)})


@app.get("/api/stream")
async def api_stream() -> StreamingResponse:
    async def gen():
        # Heartbeat so proxies don't reap the connection.
        yield ": connected\n\n"
        async for evt in subscribe():
            yield f"data: {json.dumps(evt)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Email Agent — Stats</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0e0f12;
    --panel: #16181d;
    --panel-2: #1c1f26;
    --text: #e6e7ea;
    --muted: #8a8f9a;
    --accent: #6b8afd;
    --border: #262932;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  h1 { font-size: 20px; margin: 0 0 4px 0; }
  .sub { color: var(--muted); margin-bottom: 24px; }
  .grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 24px;
  }
  .stat {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
  }
  .stat .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .stat .value { font-size: 28px; font-weight: 600; margin-top: 6px; }
  .row { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 24px; }
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
  }
  .panel h2 { font-size: 14px; margin: 0 0 12px 0; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    background: var(--panel-2); color: var(--text);
    font-size: 11px;
  }
  .feed { display: flex; flex-direction: column; gap: 8px; max-height: 480px; overflow-y: auto; }
  .card {
    border-left: 3px solid var(--accent);
    background: var(--panel-2);
    padding: 10px 12px;
    border-radius: 6px;
    animation: slideIn .25s ease-out;
  }
  .card .top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .card .sender { color: var(--muted); font-size: 12px; }
  .card .subject { font-weight: 500; margin-top: 2px; word-break: break-word; }
  .card .meta { color: var(--muted); font-size: 11px; margin-top: 4px; }
  @keyframes slideIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
  .badge { font-size: 10px; padding: 1px 6px; border-radius: 4px; background: rgba(255,255,255,.08); }
  .status { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #3ec48f; box-shadow: 0 0 6px #3ec48f; }
</style>
</head>
<body>
  <h1>Email Agent — Stats</h1>
  <div class="sub">SQLite-backed; live cards stream from <code>push_ui_message</code> via SSE.
    <span class="status" style="margin-left:12px"><span class="dot" id="dot"></span><span id="status-text">connecting…</span></span>
  </div>

  <div class="grid" id="stat-grid"></div>

  <div class="row">
    <div class="panel">
      <h2>Daily volume by category</h2>
      <canvas id="daily-chart" height="100"></canvas>
    </div>
    <div class="panel">
      <h2>Category breakdown</h2>
      <canvas id="cat-chart" height="180"></canvas>
    </div>
  </div>

  <div class="row">
    <div class="panel">
      <h2>Live feed (generative UI)</h2>
      <div class="feed" id="feed"></div>
    </div>
    <div class="panel">
      <h2>Top senders</h2>
      <table id="senders-table">
        <thead><tr><th>Sender</th><th>Count</th><th>Junk</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <h2>Recent events</h2>
    <table id="recent-table">
      <thead><tr><th>Time</th><th>Sender</th><th>Subject</th><th>Category</th><th>Conf.</th><th>Action</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

<script>
const CATEGORY_COLORS = {
  newsletter: "#6b8afd",
  receipt: "#8e6bfd",
  calendar: "#fd9b6b",
  personal: "#3ec48f",
  work: "#3ec4c4",
  junk: "#d35d6e",
  unknown: "#9aa0a6",
};

let dailyChart, catChart;

function setStat(label, value) {
  const el = document.createElement("div");
  el.className = "stat";
  el.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>`;
  document.getElementById("stat-grid").appendChild(el);
}

function renderStats(s) {
  const grid = document.getElementById("stat-grid");
  grid.innerHTML = "";
  setStat("Events processed", s.totals.total_events.toLocaleString());
  setStat("Unique emails", s.totals.unique_emails.toLocaleString());
  setStat("Drafts created", s.totals.drafts_created.toLocaleString());
  const topCat = s.by_category[0] ? s.by_category[0].category : "—";
  setStat("Top category", topCat);

  const days = [...new Set(s.daily.map(d => d.day))].sort();
  const cats = [...new Set(s.daily.map(d => d.category))];
  const map = {};
  for (const d of s.daily) map[`${d.day}|${d.category}`] = d.count;

  const datasets = cats.map(c => ({
    label: c,
    data: days.map(d => map[`${d}|${c}`] || 0),
    backgroundColor: CATEGORY_COLORS[c] || "#888",
    borderColor: CATEGORY_COLORS[c] || "#888",
    borderWidth: 1,
  }));

  if (dailyChart) dailyChart.destroy();
  dailyChart = new Chart(document.getElementById("daily-chart"), {
    type: "bar",
    data: { labels: days, datasets },
    options: {
      responsive: true,
      scales: { x: { stacked: true, ticks: { color: "#8a8f9a" }, grid: { color: "#262932" } },
                y: { stacked: true, ticks: { color: "#8a8f9a" }, grid: { color: "#262932" } } },
      plugins: { legend: { labels: { color: "#e6e7ea" } } },
    },
  });

  if (catChart) catChart.destroy();
  catChart = new Chart(document.getElementById("cat-chart"), {
    type: "doughnut",
    data: {
      labels: s.by_category.map(r => r.category),
      datasets: [{
        data: s.by_category.map(r => r.count),
        backgroundColor: s.by_category.map(r => CATEGORY_COLORS[r.category] || "#888"),
        borderColor: "#16181d",
      }],
    },
    options: { plugins: { legend: { labels: { color: "#e6e7ea" } } } },
  });

  const tbody = document.querySelector("#senders-table tbody");
  tbody.innerHTML = "";
  for (const r of s.top_senders) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(r.sender)}</td><td>${r.count}</td><td>${r.junk_count}</td>`;
    tbody.appendChild(tr);
  }
}

function renderRecent(events) {
  const tbody = document.querySelector("#recent-table tbody");
  tbody.innerHTML = "";
  for (const e of events) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${e.ts.slice(11,19)}</td>
      <td>${escapeHtml(e.sender)}</td>
      <td>${escapeHtml(e.subject)}</td>
      <td><span class="pill" style="background:${CATEGORY_COLORS[e.category] || "#888"}22;color:${CATEGORY_COLORS[e.category] || "#fff"}">${e.category}</span></td>
      <td>${e.confidence.toFixed(2)}</td>
      <td>${escapeHtml(e.action_notes || "")}</td>
    `;
    tbody.appendChild(tr);
  }
}

function pushCard(props) {
  const feed = document.getElementById("feed");
  const card = document.createElement("div");
  card.className = "card";
  card.style.borderLeftColor = props.accent_color || "#6b8afd";
  card.innerHTML = `
    <div class="top">
      <span class="sender">${escapeHtml(props.sender)}</span>
      <span class="badge" style="background:${(props.accent_color || "#6b8afd")}33;color:${props.accent_color || "#6b8afd"}">${props.category}</span>
    </div>
    <div class="subject">${escapeHtml(props.subject)}</div>
    <div class="meta">
      ${props.ts.slice(11,19)} · conf ${Number(props.confidence).toFixed(2)} ·
      ${escapeHtml(props.action_notes || "")}
      ${props.draft_created ? '<span class="badge" style="margin-left:6px;background:#3ec48f22;color:#3ec48f">draft</span>' : ""}
    </div>
  `;
  feed.prepend(card);
  while (feed.children.length > 25) feed.lastChild.remove();
}

function escapeHtml(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function refresh() {
  const [stats, recent] = await Promise.all([
    fetch("/api/stats").then(r => r.json()),
    fetch("/api/events?limit=50").then(r => r.json()),
  ]);
  renderStats(stats);
  renderRecent(recent.events);
}

function connectStream() {
  const es = new EventSource("/api/stream");
  const dot = document.getElementById("dot");
  const txt = document.getElementById("status-text");
  es.onopen = () => { dot.style.background = "#3ec48f"; dot.style.boxShadow = "0 0 6px #3ec48f"; txt.textContent = "live"; };
  es.onerror = () => { dot.style.background = "#d35d6e"; dot.style.boxShadow = "0 0 6px #d35d6e"; txt.textContent = "reconnecting"; };
  es.onmessage = (msg) => {
    try {
      const evt = JSON.parse(msg.data);
      if (evt.type === "email_processed") {
        pushCard(evt.props);
        refresh();
      }
    } catch (e) { console.error(e); }
  };
}

refresh();
connectStream();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)
