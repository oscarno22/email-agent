"""One-off backfill of historical JSONL action logs into SQLite.

Run from src/agent (uv run python -m agent.backfill) or via the Makefile.
Idempotent: the (ts, gmail_id) UNIQUE index makes re-runs no-ops.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent.db import init_db, record_event

_LOG_DIR = Path(__file__).parent.parent / "logs"


def _domain_from_sender(sender: str) -> str:
    if "@" in sender:
        return sender.rsplit("@", 1)[-1].rstrip(">").strip().lower()
    return ""


def main() -> int:
    init_db()
    if not _LOG_DIR.exists():
        print(f"no log directory at {_LOG_DIR}")
        return 0

    files = sorted(_LOG_DIR.glob("*.jsonl"))
    inserted = 0
    skipped = 0
    for path in files:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue

                sender = entry.get("sender", "")
                record_event(
                    ts=entry["ts"],
                    gmail_id=entry["gmail_id"],
                    thread_id=None,
                    sender=sender,
                    sender_domain=_domain_from_sender(sender),
                    subject=entry.get("subject", ""),
                    category=entry.get("category", "unknown"),
                    confidence=float(entry.get("confidence", 0.0)),
                    action_notes=entry.get("action", ""),
                    trust_phase=entry.get("trust_phase", "shadow"),
                    draft_created=bool(entry.get("draft_created", False)),
                )
                inserted += 1
        print(f"processed {path.name}")

    print(f"done — {inserted} rows attempted, {skipped} malformed lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
