import logging
import os

from agent.ingestion.gmail_client import register_watch

logger = logging.getLogger(__name__)

TOPIC = os.getenv("PUBSUB_TOPIC_NAME", "projects/email-agent-ozzy/topics/gmail-push")


def main() -> None:
    result = register_watch(TOPIC)
    expiration = result.get("expiration")
    history_id = result.get("historyId")
    logger.info(
        "[renew_watch] renewed — historyId=%s expiration=%s",
        history_id,
        expiration,
    )
    # Persist the expiry so /status can show when push next needs renewing, even
    # across restarts. Best-effort — never fail the renewal over a status write.
    try:
        from agent.ingestion import runtime_state
        from agent.stats.db import kv_set

        if expiration:
            kv_set("watch_expiration", str(expiration))
            runtime_state.update(watch_expiration=str(expiration))
        runtime_state.update(last_renew_status="ok")
    except Exception:
        logger.exception("[renew_watch] failed to persist watch expiration")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
