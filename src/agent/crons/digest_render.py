"""MJML → HTML rendering for the digest emails.

Both the daily digest (`crons/digest.py`) and the quick digest
(`crons/quick_digest.py`) send a plaintext body as the fallback and an HTML
alternative built here. MJML is compiled in-process via `mjml-python` (Rust
`mrml` bindings) — no Node toolchain required.

All sender/subject/notes values originate from arbitrary email and are escaped
with `html.escape` before being injected into the markup.
"""

import html as _html
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from mjml import mjml2html

logger = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")

# One badge color per Category value (core/state.py). Fresh palette — the
# dashboard has no per-category colors to reuse.
_CATEGORY_COLORS = {
    "work": "#2563eb",
    "personal": "#16a34a",
    "receipt": "#d97706",
    "newsletter": "#7c3aed",
    "calendar": "#0891b2",
    "junk": "#6b7280",
    "unknown": "#dc2626",
}
_DEFAULT_COLOR = "#6b7280"


def _esc(s: Any) -> str:
    return _html.escape(str(s if s is not None else ""))


def _color(category: str | None) -> str:
    return _CATEGORY_COLORS.get((category or "unknown").lower(), _DEFAULT_COLOR)


def _badge(category: str | None) -> str:
    cat = (category or "unknown").lower()
    return (
        f'<span style="display:inline-block;background:{_color(cat)};color:#ffffff;'
        f"border-radius:4px;padding:2px 8px;font-size:11px;font-weight:600;"
        f'text-transform:uppercase;letter-spacing:.03em;white-space:nowrap;">'
        f"{_esc(cat)}</span>"
    )


def _chip(category: str | None, count: int) -> str:
    cat = (category or "unknown").lower()
    return (
        f'<span style="display:inline-block;background:{_color(cat)};color:#ffffff;'
        f"border-radius:12px;padding:3px 10px;margin:0 6px 6px 0;font-size:12px;"
        f'font-weight:600;white-space:nowrap;">{count} {_esc(cat)}</span>'
    )


def _fmt_time(ts: str | None) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone(_EASTERN).strftime("%H:%M")  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return ts[11:16] if ts and len(ts) >= 16 else "??:??"


def _entry_action(e: dict[str, Any]) -> str:
    """Action summary for a stored event dict — mirrors digest.py / quick_digest.py."""
    extras = []
    if e.get("draft_created"):
        extras.append("draft created")
    notes = (e.get("action_notes") or "").strip()
    if notes:
        extras.append(notes)
    return "; ".join(extras)


# --- table ---------------------------------------------------------------

_TD = "padding:9px 8px;font-size:13px;border-bottom:1px solid #eef0f4;vertical-align:top;"
_TH = (
    "padding:6px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;"
    "color:#9ca3af;text-align:left;border-bottom:2px solid #eef0f4;"
)


def _row(r: dict[str, Any]) -> str:
    action = _esc(r.get("action"))
    action_cell = f'<span style="color:#6b7280;">{action}</span>' if action else ""
    return (
        "<tr>"
        f'<td style="{_TD}color:#6b7280;white-space:nowrap;">{_esc(r.get("time"))}</td>'
        f'<td style="{_TD}">{_badge(r.get("category"))}</td>'
        f'<td style="{_TD}color:#374151;">{_esc(r.get("sender"))}</td>'
        f'<td style="{_TD}color:#111827;font-weight:500;">{_esc(r.get("subject"))}</td>'
        f'<td style="{_TD}font-size:12px;">{action_cell}</td>'
        "</tr>"
    )


def _table(rows: list[dict[str, Any]]) -> str:
    header = (
        "<tr>"
        f'<th style="{_TH}">Time</th>'
        f'<th style="{_TH}">Category</th>'
        f'<th style="{_TH}">Sender</th>'
        f'<th style="{_TH}">Subject</th>'
        f'<th style="{_TH}">Action</th>'
        "</tr>"
    )
    body = "".join(_row(r) for r in rows)
    return f'<mj-table cellpadding="0" cellspacing="0" font-size="13px">{header}{body}</mj-table>'


# --- document wrapper ----------------------------------------------------


def _document(title: str, subtitle: str, sections: str) -> str:
    """Wrap pre-built MJML sections in a full document and compile to HTML."""
    mjml = f"""
<mjml>
  <mj-head>
    <mj-attributes>
      <mj-all font-family="-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif" />
    </mj-attributes>
    <mj-preview>{_esc(subtitle)}</mj-preview>
  </mj-head>
  <mj-body background-color="#f4f5f7">
    <mj-section padding="24px 24px 4px 24px">
      <mj-column>
        <mj-text font-size="22px" font-weight="700" color="#111827"
                 padding="0">{_esc(title)}</mj-text>
        <mj-text font-size="13px" color="#6b7280" padding="4px 0 0 0">{_esc(subtitle)}</mj-text>
      </mj-column>
    </mj-section>
    {sections}
  </mj-body>
</mjml>
"""
    return mjml2html(mjml)


# --- public API ----------------------------------------------------------


def render_daily_digest(
    entries: list[dict[str, Any]],
    counts: dict[str, int],
    unknowns: list[dict[str, Any]],
    date_str: str,
) -> str:
    """Render the daily digest HTML from stored event dicts (`get_events_for_date`)."""
    chips = "".join(_chip(cat, n) for cat, n in sorted(counts.items(), key=lambda x: -x[1]))
    rows = [
        {
            "time": _fmt_time(e.get("ts")),
            "category": e.get("category"),
            "sender": e.get("sender"),
            "subject": e.get("subject") or "(no subject)",
            "action": _entry_action(e),
        }
        for e in entries
    ]

    sections = f"""
    <mj-section padding="8px 24px 0 24px">
      <mj-column>
        <mj-text padding="0">{chips}</mj-text>
      </mj-column>
    </mj-section>
    <mj-section background-color="#ffffff" border-radius="8px" padding="8px 16px" css-class="card">
      <mj-column>
        {_table(rows)}
      </mj-column>
    </mj-section>
"""

    if unknowns:
        items = "".join(
            f'<tr><td style="{_TD}color:#374151;">{_esc(e.get("sender"))}</td>'
            f'<td style="{_TD}color:#111827;">{_esc(e.get("subject") or "(no subject)")}</td></tr>'
            for e in unknowns
        )
        sections += f"""
    <mj-section padding="16px 24px 0 24px">
      <mj-column>
        <mj-text font-size="14px" font-weight="600" color="#b91c1c" padding="0 0 8px 0">
          {len(unknowns)} email(s) left for manual review
        </mj-text>
        <mj-table cellpadding="0" cellspacing="0" font-size="13px">{items}</mj-table>
      </mj-column>
    </mj-section>
"""

    subtitle = f"{date_str} · {len(entries)} email(s) processed"
    return _document("Daily Digest", subtitle, sections)


def render_quick_digest(rows: list[dict[str, Any]], *, subtitle: str) -> str:
    """Render the quick digest HTML.

    `rows` are normalized dicts with keys time/category/sender/subject/action.
    An empty `rows` renders the "all clear" variant (heading + subtitle only).
    """
    if rows:
        sections = f"""
    <mj-section background-color="#ffffff" border-radius="8px" padding="8px 16px">
      <mj-column>
        {_table(rows)}
      </mj-column>
    </mj-section>
"""
    else:
        sections = """
    <mj-section padding="8px 24px 0 24px">
      <mj-column>
        <mj-text font-size="14px" color="#16a34a" padding="0">
          ✓ All clear — nothing new in your inbox.
        </mj-text>
      </mj-column>
    </mj-section>
"""
    return _document("Quick Digest", subtitle, sections)


__all__ = ["render_daily_digest", "render_quick_digest"]
