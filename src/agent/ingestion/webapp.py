import asyncio
import base64
import json
import logging
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, Response
from langgraph_sdk import get_client

from agent.ingestion.gmail_client import fetch_email, list_history

logging.getLogger("agent").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()

_VERIFICATION_TOKEN = os.getenv("PUBSUB_VERIFICATION_TOKEN", "")
_LANGGRAPH_URL = os.getenv("LANGGRAPH_URL", "http://localhost:2024")
_TRUST_PHASE = os.getenv("TRUST_PHASE", "label")

# Persists the last-seen historyId across requests so history.list always gets
# the right startHistoryId (the notification's historyId is the NEW state, not the start).
_HISTORY_ID_FILE = Path(__file__).parent.parent.parent / "last_history_id.txt"
_GMAIL_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # UUID URL namespace


def _gmail_thread_uuid(gmail_thread_id: str) -> str:
    """Deterministic UUID derived from a Gmail thread ID."""
    return str(uuid.uuid5(_GMAIL_NS, gmail_thread_id))


def _read_history_id() -> str | None:
    if _HISTORY_ID_FILE.exists():
        return _HISTORY_ID_FILE.read_text().strip() or None
    return None


def _write_history_id(history_id: str) -> None:
    _HISTORY_ID_FILE.write_text(history_id)


@app.post("/webhook/pubsub")
async def pubsub_webhook(request: Request, token: str = "") -> Response:
    if _VERIFICATION_TOKEN and token != _VERIFICATION_TOKEN:
        logger.warning("[webhook] rejected request — bad verification token")
        return Response(status_code=403)

    try:
        body = await request.json()
        data_b64 = body["message"]["data"]
        data = json.loads(base64.b64decode(data_b64 + "==").decode())
        history_id = str(data["historyId"])
    except Exception as exc:
        logger.error("[webhook] malformed pub/sub message: %s", exc)
        return Response(status_code=400)

    start_id = _read_history_id() or str(int(history_id) - 1)
    logger.info(
        "[webhook] pub/sub received historyId=%s — querying from startHistoryId=%s",
        history_id,
        start_id,
    )

    try:
        message_ids = await asyncio.to_thread(list_history, start_id)
    except Exception as exc:
        logger.error("[webhook] history.list failed: %s", exc)
        return Response(status_code=200)

    _write_history_id(history_id)

    if not message_ids:
        logger.info("[webhook] no new INBOX messages found")
        return Response(status_code=200)

    logger.info("[webhook] found %d new message(s): %s", len(message_ids), message_ids)

    client = get_client(url=_LANGGRAPH_URL)

    for message_id in message_ids:
        try:
            email_obj = await asyncio.to_thread(fetch_email, message_id)
            logger.info(
                "[webhook] fetched email — from=%s subject=%r",
                email_obj.sender,
                email_obj.subject,
            )
            thread = await client.threads.create(
                thread_id=_gmail_thread_uuid(email_obj.thread_id),
                if_exists="do_nothing",
            )
            await client.runs.create(
                thread["thread_id"],
                "agent",
                input={
                    "email": email_obj.model_dump(mode="json"),
                    "trust_phase": _TRUST_PHASE,
                },
            )
            logger.info(
                "[webhook] scheduled run — thread=%s from=%s subject=%r trust_phase=%s",
                thread["thread_id"],
                email_obj.sender,
                email_obj.subject,
                _TRUST_PHASE,
            )
        except Exception:
            logger.exception("[webhook] failed to process message %s", message_id)

    return Response(status_code=200)
