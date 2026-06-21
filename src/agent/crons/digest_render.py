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
        f"border-radius:4px;padding:3px 9px;font-size:12px;font-weight:600;"
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


# --- cards ----------------------------------------------------------------
#
# A single-column stack of per-email cards rather than a wide multi-column table:
# each card reflows to the screen width, so it stays readable on phones with no
# horizontal scroll. The cards live inside one <mj-text> (full width, no MJML
# column math). The badge + time share one row via a 100%-width inner table so
# they stay on a line together; everything else stacks vertically.


def _card(r: dict[str, Any]) -> str:
    action = _esc(r.get("action"))
    action_line = (
        f'<div style="color:#6b7280;font-size:13px;margin-top:4px;'
        f'word-break:break-word;">&#8594; {action}</div>'
        if action
        else ""
    )
    return (
        '<div style="padding:12px 0;border-bottom:1px solid #eef0f4;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        ' style="width:100%;border-collapse:collapse;"><tr>'
        f'<td style="text-align:left;vertical-align:middle;">{_badge(r.get("category"))}</td>'
        '<td style="text-align:right;vertical-align:middle;color:#6b7280;'
        f'font-size:13px;white-space:nowrap;">{_esc(r.get("time"))}</td>'
        "</tr></table>"
        f'<div style="color:#374151;font-size:13px;margin-top:6px;'
        f'word-break:break-word;">{_esc(r.get("sender"))}</div>'
        f'<div style="color:#111827;font-size:15px;font-weight:600;margin-top:2px;'
        f'word-break:break-word;">{_esc(r.get("subject"))}</div>'
        f"{action_line}"
        "</div>"
    )


def _cards(rows: list[dict[str, Any]]) -> str:
    return f'<mj-text padding="0">{"".join(_card(r) for r in rows)}</mj-text>'


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
  <mj-body background-color="#f4f5f7" width="600px">
    <mj-section padding="20px 16px 4px 16px">
      <mj-column>
        <mj-text font-size="22px" font-weight="700" color="#111827"
                 padding="0">{_esc(title)}</mj-text>
        <mj-text font-size="14px" color="#6b7280" padding="4px 0 0 0">{_esc(subtitle)}</mj-text>
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
    <mj-section padding="8px 16px 0 16px">
      <mj-column>
        <mj-text padding="0">{chips}</mj-text>
      </mj-column>
    </mj-section>
    <mj-section background-color="#ffffff" border-radius="8px" padding="4px 16px" css-class="card">
      <mj-column>
        {_cards(rows)}
      </mj-column>
    </mj-section>
"""

    if unknowns:
        items = "".join(
            '<div style="padding:8px 0;border-bottom:1px solid #eef0f4;">'
            f'<div style="color:#374151;font-size:13px;word-break:break-word;">'
            f"{_esc(e.get('sender'))}</div>"
            f'<div style="color:#111827;font-size:14px;word-break:break-word;">'
            f"{_esc(e.get('subject') or '(no subject)')}</div>"
            "</div>"
            for e in unknowns
        )
        sections += f"""
    <mj-section padding="16px 16px 0 16px">
      <mj-column>
        <mj-text font-size="14px" font-weight="600" color="#b91c1c" padding="0 0 8px 0">
          {len(unknowns)} email(s) left for manual review
        </mj-text>
        <mj-text padding="0">{items}</mj-text>
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
    <mj-section background-color="#ffffff" border-radius="8px" padding="4px 16px">
      <mj-column>
        {_cards(rows)}
      </mj-column>
    </mj-section>
"""
    else:
        sections = """
    <mj-section padding="8px 16px 0 16px">
      <mj-column>
        <mj-text font-size="15px" color="#16a34a" padding="0">
          ✓ All clear — nothing new in your inbox.
        </mj-text>
      </mj-column>
    </mj-section>
"""
    return _document("Quick Digest", subtitle, sections)


__all__ = ["render_daily_digest", "render_quick_digest"]
