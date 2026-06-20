import logging
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agent.crons.digest_render import render_quick_digest
from agent.ingestion.gmail_client import apply_action, fetch_email, search_messages, send_email
from agent.stats.db import get_event_for_gmail_id, kv_get, kv_set

logger = logging.getLogger(__name__)

_DIGEST_TO = os.getenv("DIGEST_TO_EMAIL", "oscarnolen@gmail.com")
_QUICK_DIGEST_LABEL = "Email Agent/Quick Digest"
_CURSOR_KEY = "last_quick_digest_ts"
_DEFAULT_WINDOW = timedelta(hours=3)
_OVERLAP_BUFFER = timedelta(seconds=60)
_EASTERN = ZoneInfo("America/New_York")


def _action_summary(event: dict | None) -> str:
    """Short description of what the agent did, from the stored event (best-effort)."""
    if not event:
        return ""
    extras = []
    if event.get("draft_created"):
        extras.append("draft created")
    notes = (event.get("action_notes") or "").strip()
    if notes:
        extras.append(notes)
    return "; ".join(extras)


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
        event = None
        try:
            event = get_event_for_gmail_id(msg_id)
        except Exception:
            logger.warning("[quick_digest] event lookup failed for %s", msg_id, exc_info=True)
        emails.append((email, event))
    emails.sort(key=lambda pair: pair[0].received_at)

    now_et = now.astimezone(_EASTERN)
    subject = f"Email Agent — Quick Digest ({now_et:%-I:%M %p ET})"

    rows: list[dict] = []
    if not emails:
        # Always send an "all clear" so an empty run still confirms the cron is alive.
        last_run_et = last_run.astimezone(_EASTERN)
        subject += " — all clear"
        subtitle = f"No new inbox mail since {last_run_et:%-I:%M %p ET}."
        lines = [subject, subtitle]
        logger.info("[quick_digest] no new mail since %s — sending all-clear", last_run.isoformat())
    else:
        subtitle = f"{len(emails)} new email(s) in your inbox"
        lines = [subject, f"{subtitle}:", ""]
        for email, event in emails:
            received_et = email.received_at.astimezone(_EASTERN)
            category = event.get("category") if event else None
            tag = f"   |   {category}" if category else ""
            action = _action_summary(event)
            outcome = f"   →   {action}" if action else ""
            lines.append(
                f"  {received_et:%H:%M}  |  {email.sender}  |  {email.subject}{tag}{outcome}"
            )
            rows.append(
                {
                    "time": f"{received_et:%H:%M}",
                    "category": category,
                    "sender": email.sender,
                    "subject": email.subject,
                    "action": action,
                }
            )

    # HTML is best-effort — a render failure must never block the digest.
    html = None
    try:
        html = render_quick_digest(rows, subtitle=subtitle)
    except Exception:
        logger.warning("[quick_digest] HTML render failed — sending plaintext only", exc_info=True)

    # Send first; only advance the cursor on success so a send failure re-covers
    # the window next run (bias toward duplicates over missed mail).
    msg_id = send_email(to=_DIGEST_TO, subject=subject, body="\n".join(lines), html=html)
    kv_set(_CURSOR_KEY, str(int(now.timestamp())))
    logger.info("[quick_digest] sent %d email(s) to %s", len(emails), _DIGEST_TO)

    # The email already went out — a label failure must not undo that.
    try:
        apply_action(msg_id, [_QUICK_DIGEST_LABEL], archive=False)
    except Exception:
        logger.warning("[quick_digest] could not label digest email %s", msg_id, exc_info=True)


if __name__ == "__main__":
    main()
