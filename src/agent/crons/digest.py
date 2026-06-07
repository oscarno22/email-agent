import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.core.state import Category
from agent.ingestion.gmail_client import send_email

_LOG_DIR = Path(__file__).parent.parent.parent / "logs"
_DIGEST_TO = os.getenv("DIGEST_TO_EMAIL", "oscarnolen@gmail.com")


def main() -> None:
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    log_file = _LOG_DIR / f"{yesterday}.jsonl"

    if not log_file.exists():
        print(f"No log for {yesterday} — nothing to send.")
        return

    entries = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    if not entries:
        print(f"Empty log for {yesterday} — nothing to send.")
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
    print(f"Digest sent to {_DIGEST_TO}")


if __name__ == "__main__":
    main()
