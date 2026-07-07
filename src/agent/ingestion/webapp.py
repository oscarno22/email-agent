import asyncio
import base64
import json
import logging
import os
import sys
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request, Response

from agent.core.graph import graph
from agent.core.llm import LLMError
from agent.core.nodes import is_own_digest
from agent.core.state import Email, State, TrustPhase
from agent.crons import digest, quick_digest, renew_watch
from agent.ingestion import runtime_state
from agent.ingestion.auth import require_dashboard_auth
from agent.ingestion.gmail_client import (
    apply_action,
    check_credentials,
    fetch_email,
    list_history,
    send_email,
)
from agent.stats.dashboard import router as _dashboard_router
from agent.stats.db import (
    get_cursor,
    init_db,
    kv_get,
    kv_set,
    message_already_processed,
    set_cursor,
)
from agent.stats.events import attach_loop

load_dotenv()


def _configure_logging() -> None:
    """Install a root stdout handler so the app's own logs reach CloudWatch.

    Without this, only uvicorn's access logs surface and every `agent.*` line
    (webhook, gmail, lifespan) is dropped by Python's last-resort handler.
    `force=True` ensures we own the root handler regardless of uvicorn's setup;
    uvicorn configures only its own loggers, so they keep working.
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("agent").setLevel(level)


_configure_logging()
logger = logging.getLogger(__name__)

_VERIFICATION_TOKEN = os.getenv("PUBSUB_VERIFICATION_TOKEN", "")
_TRUST_PHASE = TrustPhase(os.getenv("TRUST_PHASE", "label"))
_ENABLE_CRONS = os.getenv("ENABLE_CRONS", "false").lower() == "true"

# AI-failure fallback. Reached only when *every* configured provider fails
# (e.g. Anthropic credits exhausted AND Gemini quota/key bad). On failure the
# email is left untouched and filed under a review label; an alert is emailed
# at most once an hour so an outage doesn't spam one message per email.
# `or` (not getenv's default arg) so a present-but-empty env var still falls back.
_DIGEST_TO = os.getenv("DIGEST_TO_EMAIL") or "oscarnolen@gmail.com"
_AI_FAILURE_LABEL = "Email Agent/Needs Review"
_AI_ALERT_LABEL = "Email Agent/Alerts"
_AI_ALERT_KEY = "last_ai_alert_ts"
_AI_ALERT_THROTTLE = 3600  # seconds between alert emails

# Webhook → worker hand-off. The webhook enqueues and returns 200 immediately
# (Pub/Sub redelivers on a slow ack); a single worker drains the queue and runs
# the graph in-process, keeping cursor/dedup handling serialized.
_queue: asyncio.Queue[Email] = asyncio.Queue(maxsize=500)


def _handle_ai_failure(email: Email, exc: Exception) -> None:
    """Fallback when classification fails (Anthropic token expiry / outage).

    Leaves the email untouched (no normal category label/archive) and files it
    under a dedicated review label so it's findable, then emails a throttled alert
    so an outage doesn't send one alert per message. Every step is best-effort —
    nothing here may raise back into the worker.
    """
    runtime_state.mark_ai_error(str(exc))

    try:
        apply_action(email.gmail_id, [_AI_FAILURE_LABEL], archive=False)
    except Exception:
        logger.warning("[worker] could not label %s for review", email.gmail_id, exc_info=True)

    try:
        last = kv_get(_AI_ALERT_KEY)
        now = int(datetime.now(UTC).timestamp())
        if last and now - int(last) < _AI_ALERT_THROTTLE:
            return
        body = (
            "Email Agent could not classify incoming mail — every configured LLM "
            "provider failed.\n\n"
            "Most likely causes: ANTHROPIC_API_KEY is expired/invalid AND "
            "GEMINI_API_KEY is missing/exhausted (or both APIs are down at once).\n\n"
            f"Error: {exc}\n\n"
            f"Affected emails are left untouched in your inbox under "
            f"'{_AI_FAILURE_LABEL}' for manual review. Classification resumes "
            "automatically once at least one provider key is fixed."
        )
        msg_id = send_email(
            to=_DIGEST_TO,
            subject="⚠️ Email Agent — AI classification failing",
            body=body,
        )
        kv_set(_AI_ALERT_KEY, str(now))
        try:
            apply_action(msg_id, [_AI_ALERT_LABEL], archive=False)
        except Exception:
            logger.warning("[worker] could not label AI alert %s", msg_id, exc_info=True)
    except Exception:
        logger.exception("[worker] failed to send AI-failure alert")


async def _worker() -> None:
    while True:
        email = await _queue.get()
        try:
            if is_own_digest(email):
                # Our own digest landing back in the inbox: don't spend an LLM call
                # classifying it — just archive it. This is the reliable point to
                # archive (INBOX is definitely set by now), backing up the
                # send-time archive in the digest crons, which can race delivery.
                try:
                    await asyncio.to_thread(apply_action, email.gmail_id, [], archive=True)
                except Exception:
                    logger.warning(
                        "[worker] could not archive own digest %s", email.gmail_id, exc_info=True
                    )
                continue
            await graph.ainvoke(State(email=email, trust_phase=_TRUST_PHASE))
            runtime_state.touch_email_processed()
            runtime_state.mark_ai_ok()
        except LLMError as exc:
            logger.error(
                "[worker] AI classification failed for gmail_id=%s from=%s — %s",
                email.gmail_id,
                email.sender,
                exc,
            )
            await asyncio.to_thread(_handle_ai_failure, email, exc)
        except Exception:
            logger.exception(
                "[worker] graph run failed for gmail_id=%s from=%s subject=%r",
                email.gmail_id,
                email.sender,
                email.subject,
            )
        finally:
            _queue.task_done()


async def _renew_watch_job() -> None:
    await asyncio.to_thread(renew_watch.main)


async def _digest_job() -> None:
    await asyncio.to_thread(digest.main)


async def _quick_digest_job() -> None:
    await asyncio.to_thread(quick_digest.main)


def _start_scheduler():
    """Start APScheduler with watch-renewal + daily + quick digest jobs."""
    from zoneinfo import ZoneInfo

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(_renew_watch_job, CronTrigger(day="*/6", hour=9), id="renew_watch")
    scheduler.add_job(_digest_job, CronTrigger(hour=7), id="digest")
    # Quick digest every 3h during waking hours, in Eastern time (tracks DST).
    scheduler.add_job(
        _quick_digest_job,
        CronTrigger(hour="7,10,13,16,19,22", timezone=ZoneInfo("America/New_York")),
        id="quick_digest",
    )
    scheduler.start()
    return scheduler


@asynccontextmanager
async def _lifespan(app: FastAPI):
    runtime_state.mark_started()
    logger.info(
        "[lifespan] starting — trust_phase=%s crons=%s db=%s",
        _TRUST_PHASE.value,
        _ENABLE_CRONS,
        os.getenv("STATS_DB_PATH", "<default>"),
    )

    await asyncio.to_thread(check_credentials)
    logger.info("[lifespan] Gmail credentials present")

    try:
        await asyncio.to_thread(init_db)
    except Exception:
        logger.exception("[lifespan] init_db FAILED — cannot persist stats/cursor")
        raise
    logger.info("[lifespan] db ready")

    attach_loop(asyncio.get_running_loop())

    worker = asyncio.create_task(_worker())
    logger.info("[lifespan] worker started")

    scheduler = None
    if _ENABLE_CRONS:
        # Refresh the 7-day Gmail watch on every deploy/restart, then schedule.
        # A failure here (e.g. an expired/revoked refresh token) must NOT take the
        # whole service down — otherwise the task crash-loops and the dashboard +
        # /health become unreachable (and the ngrok sidecar dies with it). Log it
        # and keep serving so the problem stays diagnosable; the scheduled job will
        # retry the renewal once credentials are fixed.
        try:
            await asyncio.to_thread(renew_watch.main)
            logger.info("[lifespan] initial Gmail watch renewal OK")
        except Exception as exc:
            runtime_state.update(last_renew_status=f"failed: {exc}")
            logger.exception(
                "[lifespan] initial Gmail watch renewal FAILED — continuing to serve. "
                "If this is an auth error, run `make refresh-token`, update secrets, and "
                "redeploy; the scheduled job will retry."
            )
        scheduler = _start_scheduler()
        logger.info("[lifespan] scheduler started (watch renewal + daily + quick digest)")
    else:
        logger.info("[lifespan] ENABLE_CRONS is not true — skipping watch renewal + scheduler")

    logger.info("[lifespan] startup OK — listening on :2024")
    try:
        yield
    finally:
        logger.info("[lifespan] shutting down")
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        # Drain in-flight emails before tearing the worker down on redeploy.
        await _queue.join()
        worker.cancel()
        logger.info("[lifespan] shutdown complete")


app = FastAPI(lifespan=_lifespan)
app.include_router(_dashboard_router, dependencies=[Depends(require_dashboard_auth)])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status", dependencies=[Depends(require_dashboard_auth)])
async def status() -> dict[str, Any]:
    """At-a-glance runtime health — Gmail token, last activity, queue, watch expiry.

    Gated behind dashboard auth (it reveals token state); `/health` stays the open
    trivial check for ECS.
    """
    snap = runtime_state.snapshot()
    snap["trust_phase"] = _TRUST_PHASE.value
    snap["enable_crons"] = _ENABLE_CRONS
    snap["queue_depth"] = _queue.qsize()

    # Fall back to the persisted watch expiry if this process hasn't renewed yet.
    if snap.get("watch_expiration") is None:
        snap["watch_expiration"] = await asyncio.to_thread(kv_get, "watch_expiration")

    exp = snap.get("watch_expiration")
    if exp:
        with suppress(ValueError, TypeError):
            snap["watch_expiration_iso"] = datetime.fromtimestamp(
                int(exp) / 1000, tz=UTC
            ).isoformat()

    return snap


@app.post("/webhook/pubsub")
async def pubsub_webhook(request: Request, token: str = "") -> Response:
    if _VERIFICATION_TOKEN and token != _VERIFICATION_TOKEN:
        logger.warning("[webhook] rejected request — bad verification token")
        return Response(status_code=403)

    runtime_state.touch_webhook()

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
