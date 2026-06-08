"""Batch review: fetch unread inbox messages and run each through the triage agent."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from langgraph_sdk import get_client

from agent.core.state import TrustPhase
from agent.ingestion.gmail_client import fetch_email, list_unread, mark_as_read
from agent.stats.events import publish

logger = logging.getLogger(__name__)

_GMAIL_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def _gmail_thread_uuid(gmail_thread_id: str) -> str:
    return str(uuid.uuid5(_GMAIL_NS, gmail_thread_id))


@dataclass
class BatchStatus:
    running: bool = False
    total: int = 0
    processed: int = 0
    failed: int = 0
    last_run_ts: str | None = None
    last_run_summary: str | None = None


# Single mutable instance — mutated in-place so imports always see current state.
_status = BatchStatus()


async def run_batch_review(
    trust_phase: TrustPhase = TrustPhase.LABEL,
    mark_read: bool = True,
    max_emails: int = 50,
    langgraph_url: str = "http://localhost:2024",
) -> BatchStatus:
    """Fetch unread inbox messages and run each through the triage graph."""
    if _status.running:
        return _status

    _status.running = True
    _status.total = 0
    _status.processed = 0
    _status.failed = 0

    try:
        message_ids = await asyncio.to_thread(list_unread, max_emails)
        _status.total = len(message_ids)
        await publish({"type": "batch_start", "props": {"total": _status.total}})
        logger.info(
            "[batch] starting: %d unread messages, trust_phase=%s, mark_read=%s",
            _status.total,
            trust_phase.value,
            mark_read,
        )

        client = get_client(url=langgraph_url)

        for msg_id in message_ids:
            try:
                email_obj = await asyncio.to_thread(fetch_email, msg_id)
                thread = await client.threads.create(
                    thread_id=_gmail_thread_uuid(email_obj.thread_id),
                    if_exists="do_nothing",
                )
                await client.runs.create(
                    thread["thread_id"],
                    "agent",
                    input={
                        "email": email_obj.model_dump(mode="json"),
                        "trust_phase": trust_phase.value,
                    },
                )
                if mark_read and trust_phase != TrustPhase.SHADOW:
                    await asyncio.to_thread(mark_as_read, msg_id)
                _status.processed += 1
                logger.debug(
                    "[batch] %d/%d — %s %r",
                    _status.processed,
                    _status.total,
                    email_obj.sender,
                    email_obj.subject,
                )
            except Exception:
                logger.exception("[batch] failed to process message %s", msg_id)
                _status.failed += 1

            await publish(
                {
                    "type": "batch_progress",
                    "props": {"processed": _status.processed, "total": _status.total},
                }
            )

        _status.running = False
        _status.last_run_ts = datetime.now(tz=UTC).isoformat()
        _status.last_run_summary = f"Processed {_status.processed}/{_status.total}" + (
            f", {_status.failed} failed" if _status.failed else ""
        )
        await publish(
            {
                "type": "batch_complete",
                "props": {
                    "processed": _status.processed,
                    "total": _status.total,
                    "failed": _status.failed,
                },
            }
        )
        logger.info("[batch] complete — %s", _status.last_run_summary)
    except Exception:
        _status.running = False
        logger.exception("[batch] unexpected error")
        raise

    return _status
