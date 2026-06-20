import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request, Response

from agent.core.graph import graph
from agent.core.state import Email, State, TrustPhase
from agent.crons import digest, renew_watch
from agent.ingestion.auth import require_dashboard_auth
from agent.ingestion.gmail_client import check_credentials, fetch_email, list_history
from agent.stats.dashboard import router as _dashboard_router
from agent.stats.db import get_cursor, init_db, message_already_processed, set_cursor
from agent.stats.events import attach_loop

load_dotenv()

logging.getLogger("agent").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

_VERIFICATION_TOKEN = os.getenv("PUBSUB_VERIFICATION_TOKEN", "")
_TRUST_PHASE = TrustPhase(os.getenv("TRUST_PHASE", "draft"))
_ENABLE_CRONS = os.getenv("ENABLE_CRONS", "false").lower() == "true"

# Webhook → worker hand-off. The webhook enqueues and returns 200 immediately
# (Pub/Sub redelivers on a slow ack); a single worker drains the queue and runs
# the graph in-process, keeping cursor/dedup handling serialized.
_queue: asyncio.Queue[Email] = asyncio.Queue(maxsize=500)


async def _worker() -> None:
    while True:
        email = await _queue.get()
        try:
            await graph.ainvoke(State(email=email, trust_phase=_TRUST_PHASE))
        except Exception:
            logger.exception("[worker] graph run failed for %s", email.gmail_id)
        finally:
            _queue.task_done()


async def _renew_watch_job() -> None:
    await asyncio.to_thread(renew_watch.main)


async def _digest_job() -> None:
    await asyncio.to_thread(digest.main)


def _start_scheduler():
    """Start APScheduler with watch-renewal + daily digest jobs."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(_renew_watch_job, CronTrigger(day="*/6", hour=9), id="renew_watch")
    scheduler.add_job(_digest_job, CronTrigger(hour=7), id="digest")
    scheduler.start()
    return scheduler


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await asyncio.to_thread(check_credentials)
    await asyncio.to_thread(init_db)
    attach_loop(asyncio.get_running_loop())

    worker = asyncio.create_task(_worker())

    scheduler = None
    if _ENABLE_CRONS:
        # Refresh the 7-day Gmail watch on every deploy/restart, then schedule.
        await asyncio.to_thread(renew_watch.main)
        scheduler = _start_scheduler()
    else:
        logger.info("[lifespan] ENABLE_CRONS is not true — skipping watch renewal + scheduler")

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        # Drain in-flight emails before tearing the worker down on redeploy.
        await _queue.join()
        worker.cancel()


app = FastAPI(lifespan=_lifespan)
app.include_router(_dashboard_router, dependencies=[Depends(require_dashboard_auth)])


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

    last_id = await asyncio.to_thread(get_cursor)
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

    await asyncio.to_thread(set_cursor, history_id)

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
            await _queue.put(email_obj)
            logger.info(
                "[webhook] enqueued — from=%s subject=%r trust_phase=%s",
                email_obj.sender,
                email_obj.subject,
                _TRUST_PHASE.value,
            )
        except Exception:
            logger.exception("[webhook] failed to process message %s", message_id)

    return Response(status_code=200)
