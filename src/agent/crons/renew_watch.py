import logging
import os

from agent.ingestion.gmail_client import register_watch

logger = logging.getLogger(__name__)

TOPIC = os.getenv("PUBSUB_TOPIC_NAME", "projects/email-agent-ozzy/topics/gmail-push")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = register_watch(TOPIC)
    logger.info(
        "[renew_watch] renewed — historyId=%s expiration=%s",
        result["historyId"],
        result["expiration"],
    )
