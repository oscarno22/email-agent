import logging
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agent.ingestion.gmail_client import apply_action, fetch_email, search_messages, send_email
from agent.stats.db import get_category_for_gmail_id, kv_get, kv_set

logger = logging.getLogger(__name__)

_DIGEST_TO = os.getenv("DIGEST_TO_EMAIL", "oscarnolen@gmail.com")
_QUICK_DIGEST_LABEL = "Email Agent/Quick Digest"
_CURSOR_KEY = "last_quick_digest_ts"
_DEFAULT_WINDOW = timedelta(hours=3)
_OVERLAP_BUFFER = timedelta(seconds=60)
_EASTERN = ZoneInfo("America/New_York")


def main() -> None:
    """Send a literal, AI-independent list of inbox mail since the last run.

    Reads live from Gmail (never the classifier output) so a failed/unclassified
    email still shows up. Category is a best-effort hint that never blocks the send.
    """
    now = datetime.now(UTC)

    cursor = kv_get(_CURSOR_KEY)
    last_run = datetime.fromtimestamp(int(cursor), tz=UTC) if cursor else now - _DEFAULT_WINDOW
    bound = int((last_run - _OVERLAP_BUFFER).timestamp())

    # `in:inbox` keeps the list to what still needs attention (archived junk drops
    # off; personal/work are never archived). `-from:me` excludes our own digests.
    ids = search_messages(f"in:inbox after:{bound} -from:me")

    emails = []
    for msg_id in ids:
        email = fetch_email(msg_id)
        category = None
        try:
            category = get_category_for_gmail_id(msg_id)
        except Exception:
            logger.warning("[quick_digest] category lookup failed for %s", msg_id, exc_info=True)
        emails.append((email, category))
    emails.sort(key=lambda pair: pair[0].received_at)

    if not emails:
        logger.info("[quick_digest] no new mail since %s — nothing to send", last_run.isoformat())
        kv_set(_CURSOR_KEY, str(int(now.timestamp())))
        return

    now_et = now.astimezone(_EASTERN)
    lines = [
        f"Email Agent — Quick Digest ({now_et:%-I:%M %p ET})",
        f"{len(emails)} new email(s) in your inbox:",
        "",
    ]
    for email, category in emails:
        received_et = email.received_at.astimezone(_EASTERN)
        tag = f"   [{category}]" if category else ""
        lines.append(f"  {received_et:%H:%M}  |  {email.sender}  |  {email.subject}{tag}")

    # Send first; only advance the cursor on success so a send failure re-covers
    # the window next run (bias toward duplicates over missed mail).
    msg_id = send_email(
        to=_DIGEST_TO,
        subject=f"Email Agent — Quick Digest ({now_et:%-I:%M %p ET})",
        body="\n".join(lines),
    )
    kv_set(_CURSOR_KEY, str(int(now.timestamp())))
    logger.info("[quick_digest] sent %d email(s) to %s", len(emails), _DIGEST_TO)

    # The email already went out — a label failure must not undo that.
    try:
        apply_action(msg_id, [_QUICK_DIGEST_LABEL], archive=False)
    except Exception:
        logger.warning("[quick_digest] could not label digest email %s", msg_id, exc_info=True)


if __name__ == "__main__":
    main()
