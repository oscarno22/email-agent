import logging
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agent.core.state import Category
from agent.ingestion.gmail_client import apply_action, send_email
from agent.stats.db import get_events_for_date

logger = logging.getLogger(__name__)

_DIGEST_TO = os.getenv("DIGEST_TO_EMAIL", "oscarnolen@gmail.com")
_DAILY_DIGEST_LABEL = "Email Agent/Daily Digest"
_EASTERN = ZoneInfo("America/New_York")


def _event_line(e: dict[str, Any]) -> str:
    """Render one processed email: time | category | conf | sender | subject → action."""
    ts = e.get("ts") or ""
    try:
        when = datetime.fromisoformat(ts).astimezone(_EASTERN).strftime("%H:%M")
    except ValueError:
        when = ts[11:16] if len(ts) >= 16 else "??:??"

    conf = e.get("confidence")
    conf_str = f"{conf:.2f}" if conf is not None else " ?  "

    parts = [
        when,
        (e.get("category") or "?"),
        conf_str,
        e.get("sender") or "",
        e.get("subject") or "(no subject)",
    ]
    line = "  " + "  |  ".join(parts)

    extras = []
    if e.get("draft_created"):
        extras.append("draft created")
    notes = (e.get("action_notes") or "").strip()
    if notes:
        extras.append(notes)
    if extras:
        line += "   →   " + "; ".join(extras)
    return line


def main() -> None:
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    entries = get_events_for_date(yesterday)

    if not entries:
        logger.info("[digest] no events for %s — nothing to send", yesterday)
        return

    counts = Counter(e["category"] for e in entries)
    unknowns = [e for e in entries if e["category"] == Category.UNKNOWN.value]

    lines = [
        f"Email Agent — Daily Digest ({yesterday})",
        f"Processed {len(entries)} email(s)",
        "",
        "By category:",
    ]
    for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {n:3d}  {cat}")

    lines += ["", "All email (times in ET):"]
    for e in entries:
        lines.append(_event_line(e))

    if unknowns:
        lines += ["", f"{len(unknowns)} email(s) left for manual review:"]
        for e in unknowns:
            lines.append(f"  {e['sender']}  |  {e['subject']}")

    msg_id = send_email(
        to=_DIGEST_TO,
        subject=f"Email Agent Digest — {yesterday}",
        body="\n".join(lines),
    )
    logger.info("[digest] sent to %s", _DIGEST_TO)

    # The email already went out — a label failure must not undo that.
    try:
        apply_action(msg_id, [_DAILY_DIGEST_LABEL], archive=False)
    except Exception:
        logger.warning("[digest] could not label digest email %s", msg_id, exc_info=True)


if __name__ == "__main__":
    main()
