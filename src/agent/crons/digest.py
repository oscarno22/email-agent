import logging
import os
from collections import Counter
from datetime import UTC, datetime, timedelta

from agent.core.state import Category
from agent.ingestion.gmail_client import send_email
from agent.stats.db import get_events_for_date

logger = logging.getLogger(__name__)

_DIGEST_TO = os.getenv("DIGEST_TO_EMAIL", "oscarnolen@gmail.com")


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
    ]
    for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {n:3d}  {cat}")

    if unknowns:
        lines += ["", f"{len(unknowns)} unknown email(s) left for manual review:"]
        for e in unknowns:
            lines.append(f"  {e['sender']}  |  {e['subject']}")

    send_email(
        to=_DIGEST_TO,
        subject=f"Email Agent Digest — {yesterday}",
        body="\n".join(lines),
    )
    logger.info("[digest] sent to %s", _DIGEST_TO)


if __name__ == "__main__":
    main()
