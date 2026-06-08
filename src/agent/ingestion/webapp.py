import asyncio
import base64
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from langgraph_sdk import get_client

from agent.ingestion.gmail_client import check_credentials, fetch_email, list_history
from agent.stats.dashboard import router as _dashboard_router
from agent.stats.db import init_db, message_already_processed
from agent.stats.events import attach_loop

logging.getLogger("agent").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await asyncio.to_thread(check_credentials)
    await asyncio.to_thread(init_db)
    attach_loop(asyncio.get_running_loop())
    yield


app = FastAPI(lifespan=_lifespan)
app.include_router(_dashboard_router)

_VERIFICATION_TOKEN = os.getenv("PUBSUB_VERIFICATION_TOKEN", "")
_LANGGRAPH_URL = os.getenv("LANGGRAPH_URL", "http://localhost:2024")
_TRUST_PHASE = os.getenv("TRUST_PHASE", "label")

_GMAIL_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # UUID URL namespace
# Namespace + key used to persist the Gmail history cursor in the LangGraph store.
# In dev (langgraph dev) this lives in InMemoryStore; in production it's Postgres-backed.
_STORE_NS = ("webhook",)
_HISTORY_ID_KEY = "last_history_id"


def _gmail_thread_uuid(gmail_thread_id: str) -> str:
    """Deterministic UUID derived from a Gmail thread ID."""
    return str(uuid.uuid5(_GMAIL_NS, gmail_thread_id))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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

    client = get_client(url=_LANGGRAPH_URL)

    try:
        item = await client.store.get_item(_STORE_NS, _HISTORY_ID_KEY)
        last_id = item["value"].get("id") if item else None
    except Exception as exc:
        logger.warning("[webhook] could not read last_history_id from store: %s", exc)
        last_id = None

    start_id = last_id or str(int(history_id) - 1)
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

    try:
        await client.store.put_item(_STORE_NS, _HISTORY_ID_KEY, {"id": history_id})
    except Exception as exc:
        logger.warning("[webhook] could not persist last_history_id to store: %s", exc)

    if not message_ids:
        logger.info("[webhook] no new INBOX messages found")
        return Response(status_code=200)

    logger.info("[webhook] found %d new message(s): %s", len(message_ids), message_ids)

    for message_id in message_ids:
        try:
            email_obj = await asyncio.to_thread(fetch_email, message_id)
            logger.info(
                "[webhook] fetched email — from=%s subject=%r",
                email_obj.sender,
                email_obj.subject,
            )
            if await asyncio.to_thread(message_already_processed, email_obj.gmail_id):
                logger.info(
                    "[webhook] skipping %s — already processed (gmail_id=%s)",
                    message_id,
                    email_obj.gmail_id,
                )
                continue
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
