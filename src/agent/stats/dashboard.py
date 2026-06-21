"""Stats dashboard routes for the email agent.

Routes are exposed via `router` (an APIRouter) so they can be included in
webapp.py — giving them access to the same in-process event bus as the
ingestion webhook and therefore a working SSE live feed.

Standalone mode (``make dashboard``): the module also exports a top-level
``app`` FastAPI instance that mounts the same router. It runs on :8765 and
only has historical data (no live SSE) because it's a separate process.

Merged mode (``make start``): webapp.py includes this router, so the
dashboard is available at :2024 with full real-time SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from agent.core.state import TrustPhase
from agent.ingestion.auth import require_dashboard_auth
from agent.ingestion.batch_review import _status as _batch_status
from agent.ingestion.batch_review import run_batch_review
from agent.stats.db import (
    add_user_rule,
    delete_user_rule,
    get_category_breakdown,
    get_daily_counts,
    get_recent_events,
    get_top_senders,
    get_totals,
    get_user_rules,
    init_db,
)
from agent.stats.events import attach_loop, subscribe

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/stats")
async def api_stats() -> JSONResponse:
    totals, by_category, daily, top_senders = await asyncio.gather(
        asyncio.to_thread(get_totals),
        asyncio.to_thread(get_category_breakdown),
        asyncio.to_thread(get_daily_counts),
        asyncio.to_thread(get_top_senders),
    )
    return JSONResponse(
        {
            "totals": totals,
            "by_category": by_category,
            "daily": daily,
            "top_senders": top_senders,
        }
    )


@router.get("/api/events")
async def api_events(limit: int = 50) -> JSONResponse:
    events = await asyncio.to_thread(get_recent_events, limit)
    return JSONResponse({"events": events})


@router.get("/api/stream")
async def api_stream() -> StreamingResponse:
    async def gen():
        # Heartbeat so proxies don't reap the connection.
        yield ": connected\n\n"
        async for evt in subscribe():
            yield f"data: {json.dumps(evt)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# Trust phases ordered least → most privileged. A batch-review request may not
# escalate beyond what the deployment itself is configured to do (TRUST_PHASE).
_PHASE_RANK = {
    TrustPhase.SHADOW: 0,
    TrustPhase.LABEL: 1,
    TrustPhase.ARCHIVE: 2,
    TrustPhase.DRAFT: 3,
}


@router.post("/api/batch-review")
async def api_batch_review(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    body = await request.json()
    trust_phase_str = str(body.get("trust_phase", "label")).lower()
    try:
        trust_phase = TrustPhase(trust_phase_str)
    except ValueError:
        trust_phase = TrustPhase.LABEL

    # Clamp to the deployment's configured ceiling so an authenticated request
    # can't run a more privileged action than the process is meant to perform.
    ceiling = TrustPhase(os.getenv("TRUST_PHASE", "draft"))
    if _PHASE_RANK[trust_phase] > _PHASE_RANK[ceiling]:
        logger.warning(
            "[batch-review] clamping requested trust_phase=%s to ceiling=%s",
            trust_phase.value,
            ceiling.value,
        )
        trust_phase = ceiling

    mark_read = bool(body.get("mark_read", True))
    max_emails = min(int(body.get("max_emails", 50)), 500)

    if _batch_status.running:
        return JSONResponse(
            {
                "status": "running",
                "processed": _batch_status.processed,
                "total": _batch_status.total,
            },
            status_code=409,
        )

    background_tasks.add_task(run_batch_review, trust_phase, mark_read, max_emails)
    return JSONResponse({"status": "started"}, status_code=202)


@router.get("/api/rules")
async def api_rules_list() -> JSONResponse:
    rules = await asyncio.to_thread(get_user_rules)
    return JSONResponse({"rules": rules})


@router.post("/api/rules")
async def api_rules_add(request: Request) -> JSONResponse:
    body = await request.json()
    rule = str(body.get("rule", "")).strip()
    if not rule:
        return JSONResponse({"error": "rule text required"}, status_code=400)
    rule_id = await asyncio.to_thread(add_user_rule, rule)
    return JSONResponse({"id": rule_id, "rule": rule}, status_code=201)


@router.delete("/api/rules/{rule_id}")
async def api_rules_delete(rule_id: int) -> JSONResponse:
    deleted = await asyncio.to_thread(delete_user_rule, rule_id)
    if not deleted:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"deleted": rule_id})


@router.get("/api/batch-review/status")
async def api_batch_review_status() -> JSONResponse:
    return JSONResponse(
        {
            "running": _batch_status.running,
            "total": _batch_status.total,
            "processed": _batch_status.processed,
            "failed": _batch_status.failed,
            "last_run_ts": _batch_status.last_run_ts,
            "last_run_summary": _batch_status.last_run_summary,
        }
    )


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
  .batch-controls { display: flex; gap: 24px; flex-wrap: wrap; align-items: flex-start; margin-top: 8px; }
  .batch-field { display: flex; flex-direction: column; gap: 6px; }
  .field-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
  .btn-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .btn-opt { background: transparent; color: var(--muted); border: none; padding: 6px 14px; cursor: pointer; font-size: 13px; transition: background .15s, color .15s; }
  .btn-opt:hover { background: var(--panel-2); color: var(--text); }
  .btn-opt.active { background: var(--accent); color: #fff; }
  .check-label { color: var(--text); font-size: 13px; display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .num-input { background: var(--panel-2); border: 1px solid var(--border); color: var(--text); border-radius: 6px; padding: 6px 10px; font-size: 13px; width: 80px; }
  .run-btn { background: var(--accent); color: #fff; border: none; border-radius: 6px; padding: 8px 18px; font-size: 13px; font-weight: 500; cursor: pointer; transition: opacity .15s; }
  .run-btn:disabled { opacity: .4; cursor: not-allowed; }
  .progress-wrap { background: var(--panel-2); border-radius: 4px; height: 6px; overflow: hidden; margin-top: 16px; }
  .progress-bar { height: 100%; background: var(--accent); transition: width .3s ease; border-radius: 4px; }
  .batch-status-text { color: var(--muted); font-size: 12px; margin-top: 6px; }
  .rules-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; min-height: 24px; }
  .rule-row { display: flex; align-items: center; gap: 8px; background: var(--panel-2); border-radius: 6px; padding: 8px 10px; font-size: 13px; }
  .rule-text { flex: 1; }
  .del-btn { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 15px; line-height: 1; padding: 0 4px; }
  .del-btn:hover { color: #d35d6e; }
  .rule-add { display: flex; gap: 8px; margin-top: 4px; }
  .rule-input { flex: 1; background: var(--panel-2); border: 1px solid var(--border); color: var(--text); border-radius: 6px; padding: 6px 10px; font-size: 13px; }
  .rule-input:focus { outline: none; border-color: var(--accent); }
  .flag-btn { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 13px; padding: 2px 6px; border-radius: 4px; }
  .flag-btn:hover { background: var(--panel-2); color: var(--text); }
  .flag-panel { background: var(--panel-2); border: 1px solid var(--accent); border-radius: 8px; padding: 12px 14px; margin-top: 12px; display: none; }
  .flag-panel h3 { font-size: 13px; margin: 0 0 10px 0; color: var(--accent); }
  .flag-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
  .flag-cat-select { background: var(--panel-2); border: 1px solid var(--border); color: var(--text); border-radius: 6px; padding: 5px 8px; font-size: 13px; }
</style>
</head>
<body>
  <h1>Email Agent — Stats</h1>
  <div class="sub">SQLite-backed; live cards stream from in-process events via SSE.
    <span class="status" style="margin-left:12px"><span class="dot" id="dot"></span><span id="status-text">connecting…</span></span>
  </div>

  <div class="grid" id="stat-grid"></div>

  <div class="panel" style="margin-bottom:24px">
    <h2>Batch Review</h2>
    <div class="batch-controls">
      <div class="batch-field">
        <span class="field-label">Trust Phase</span>
        <div class="btn-group" id="trust-group">
          <button class="btn-opt" data-value="shadow">Shadow</button>
          <button class="btn-opt active" data-value="label">Label</button>
          <button class="btn-opt" data-value="draft">Draft</button>
        </div>
      </div>
      <div class="batch-field">
        <span class="field-label">Options</span>
        <label class="check-label"><input type="checkbox" id="mark-read" checked> Mark as read after processing</label>
      </div>
      <div class="batch-field">
        <span class="field-label">Max Emails</span>
        <input type="number" id="max-emails" value="50" min="1" max="500" class="num-input">
      </div>
      <div class="batch-field" style="align-self:flex-end">
        <button id="run-btn" class="run-btn" onclick="runBatch()">Run Batch Review</button>
      </div>
    </div>
    <div id="batch-progress" style="display:none">
      <div class="progress-wrap"><div id="progress-bar" class="progress-bar" style="width:0%"></div></div>
      <div id="batch-status-text" class="batch-status-text"></div>
    </div>
  </div>

  <div class="panel" style="margin-bottom:24px">
    <h2>Classification Rules</h2>
    <div class="rules-list" id="rules-list"></div>
    <div class="rule-add">
      <input type="text" id="rule-input" class="rule-input" placeholder="e.g. Emails from @corp.com are work">
      <button class="run-btn" onclick="addRule()" style="white-space:nowrap">Add Rule</button>
    </div>
    <div class="flag-panel" id="flag-panel">
      <h3 id="flag-heading">Flag misclassification</h3>
      <div class="flag-row">
        <span style="color:var(--muted);font-size:13px">Correct category:</span>
        <select id="flag-cat" class="flag-cat-select">
          <option value="newsletter">newsletter</option>
          <option value="receipt">receipt</option>
          <option value="calendar">calendar</option>
          <option value="personal">personal</option>
          <option value="work">work</option>
          <option value="banking">banking</option>
          <option value="application">application</option>
          <option value="assessment">assessment</option>
          <option value="junk">junk</option>
        </select>
      </div>
      <div class="rule-add">
        <input type="text" id="flag-rule-input" class="rule-input" placeholder="Rule suggestion — edit before saving">
        <button class="run-btn" onclick="saveFlagRule()" style="white-space:nowrap">Save Rule</button>
        <button class="flag-btn" onclick="closeFlag()" style="font-size:20px;color:var(--muted)">×</button>
      </div>
    </div>
  </div>

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
      <thead><tr><th>Time</th><th>Sender</th><th>Subject</th><th>Category</th><th>Conf.</th><th>Action</th><th></th></tr></thead>
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
    tr.dataset.sender = e.sender;
    tr.dataset.subject = e.subject;
    tr.dataset.category = e.category;
    tr.innerHTML = `
      <td>${e.ts.slice(11,19)}</td>
      <td>${escapeHtml(e.sender)}</td>
      <td>${escapeHtml(e.subject)}</td>
      <td><span class="pill" style="background:${CATEGORY_COLORS[e.category] || "#888"}22;color:${CATEGORY_COLORS[e.category] || "#fff"}">${e.category}</span></td>
      <td>${e.confidence.toFixed(2)}</td>
      <td>${escapeHtml(e.action_notes || "")}</td>
      <td><button class="flag-btn" title="Flag misclassification" onclick="openFlag(this)">🚩</button></td>
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

let _batchPhase = "label";

document.querySelectorAll("#trust-group .btn-opt").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#trust-group .btn-opt").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    _batchPhase = btn.dataset.value;
    const markRead = document.getElementById("mark-read");
    markRead.disabled = _batchPhase === "shadow";
    if (_batchPhase === "shadow") markRead.checked = false;
  });
});

function showBatchProgress(processed, total, text) {
  document.getElementById("batch-progress").style.display = "block";
  const pct = total > 0 ? Math.round((processed / total) * 100) : 0;
  document.getElementById("progress-bar").style.width = pct + "%";
  document.getElementById("batch-status-text").textContent = text;
}

async function runBatch() {
  const btn = document.getElementById("run-btn");
  const markRead = document.getElementById("mark-read").checked;
  const maxEmails = parseInt(document.getElementById("max-emails").value, 10) || 50;
  btn.disabled = true;
  showBatchProgress(0, 0, "Starting…");
  try {
    const resp = await fetch("/api/batch-review", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({trust_phase: _batchPhase, mark_read: markRead, max_emails: maxEmails}),
    });
    if (resp.status === 409) {
      document.getElementById("batch-status-text").textContent = "A run is already in progress.";
      btn.disabled = false;
    }
  } catch (e) {
    document.getElementById("batch-status-text").textContent = "Failed to start: " + e.message;
    btn.disabled = false;
  }
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
      } else if (evt.type === "batch_start") {
        showBatchProgress(0, evt.props.total, `Found ${evt.props.total} unread — processing…`);
      } else if (evt.type === "batch_progress") {
        showBatchProgress(evt.props.processed, evt.props.total, `${evt.props.processed} / ${evt.props.total} processed`);
      } else if (evt.type === "batch_complete") {
        const failed = evt.props.failed > 0 ? `, ${evt.props.failed} failed` : "";
        showBatchProgress(evt.props.processed, evt.props.total, `Done — ${evt.props.processed} processed${failed}`);
        document.getElementById("run-btn").disabled = false;
        refresh();
      }
    } catch (e) { console.error(e); }
  };
}

// ── Rules management ─────────────────────────────────────────────────────────

async function loadRules() {
  const resp = await fetch("/api/rules");
  const data = await resp.json();
  renderRules(data.rules);
}

function renderRules(rules) {
  const list = document.getElementById("rules-list");
  list.innerHTML = "";
  if (!rules.length) {
    list.innerHTML = '<span style="color:var(--muted);font-size:13px">No custom rules yet — defaults apply.</span>';
    return;
  }
  for (const r of rules) {
    const row = document.createElement("div");
    row.className = "rule-row";
    row.dataset.id = r.id;
    row.innerHTML = `<span class="rule-text">${escapeHtml(r.rule)}</span><button class="del-btn" title="Delete rule" onclick="deleteRule(${r.id})">×</button>`;
    list.appendChild(row);
  }
}

async function addRule() {
  const input = document.getElementById("rule-input");
  const rule = input.value.trim();
  if (!rule) return;
  await fetch("/api/rules", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({rule}) });
  input.value = "";
  loadRules();
}

async function deleteRule(id) {
  await fetch(`/api/rules/${id}`, { method: "DELETE" });
  loadRules();
}

document.getElementById("rule-input").addEventListener("keydown", e => { if (e.key === "Enter") addRule(); });

// ── Flag / feedback flow ──────────────────────────────────────────────────────

let _flagSender = "", _flagCategory = "";

function openFlag(btn) {
  const tr = btn.closest("tr");
  _flagSender = tr.dataset.sender || "";
  _flagCategory = tr.dataset.category || "";
  const subject = tr.dataset.subject || "";
  document.getElementById("flag-heading").textContent = `Flag: "${subject}"`;
  const catSelect = document.getElementById("flag-cat");
  // Pre-select the opposite of current to nudge the user
  catSelect.value = _flagCategory === "newsletter" ? "work" : "newsletter";
  _updateFlagSuggestion();
  catSelect.onchange = _updateFlagSuggestion;
  document.getElementById("flag-panel").style.display = "block";
  document.getElementById("flag-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function _updateFlagSuggestion() {
  const correctCat = document.getElementById("flag-cat").value;
  const domain = _flagSender.includes("@") ? _flagSender.split("@").pop() : _flagSender;
  document.getElementById("flag-rule-input").value = `Emails from @${domain} are ${correctCat}, not ${_flagCategory}`;
}

async function saveFlagRule() {
  const rule = document.getElementById("flag-rule-input").value.trim();
  if (!rule) return;
  await fetch("/api/rules", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({rule}) });
  closeFlag();
  loadRules();
}

function closeFlag() {
  document.getElementById("flag-panel").style.display = "none";
  document.getElementById("flag-rule-input").value = "";
}

document.getElementById("flag-rule-input").addEventListener("keydown", e => { if (e.key === "Enter") saveFlagRule(); });

refresh();
loadRules();
connectStream();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


@router.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


# ---------------------------------------------------------------------------
# Standalone mode — `make dashboard` / `uv run uvicorn agent.stats.dashboard:app --port 8765`
# In this mode the live SSE feed is inactive (separate process from ingestion).
# For real-time cards, use the dashboard at http://localhost:2024 when `make start` is running.
# ---------------------------------------------------------------------------

app = FastAPI(title="Email Agent — Stats")


@app.on_event("startup")
async def _startup() -> None:
    await asyncio.to_thread(init_db)
    attach_loop(asyncio.get_running_loop())


app.include_router(router, dependencies=[Depends(require_dashboard_auth)])
