"""
Register LangGraph cron jobs for watch renewal and morning digest.

Run once after the server starts:
    make setup-crons

Safe to re-run — existing crons with the same metadata tag are deleted first.
"""

import asyncio
import logging
import os

from langgraph_sdk import get_client

logger = logging.getLogger(__name__)

_URL = os.getenv("LANGGRAPH_URL", "http://localhost:2024")

_CRONS = [
    {
        "assistant_id": "renew_watch",
        "schedule": "0 9 */6 * *",  # every 6 days at 9am
        "input": {},
        "metadata": {"managed_by": "setup_crons", "name": "renew_watch"},
    },
    {
        "assistant_id": "digest",
        "schedule": "0 7 * * *",  # daily at 7am
        "input": {},
        "metadata": {"managed_by": "setup_crons", "name": "digest"},
    },
]


async def main() -> None:
    if os.getenv("ENABLE_CRONS", "false").lower() != "true":
        logger.info(
            "[setup_crons] ENABLE_CRONS is not true — skipping. "
            "Set ENABLE_CRONS=true in src/.env and re-run."
        )
        return

    client = get_client(url=_URL)

    # Remove any previously registered managed crons so re-runs are idempotent.
    existing = await client.crons.search(limit=100)
    for cron in existing:
        if cron.get("metadata", {}).get("managed_by") == "setup_crons":
            await client.crons.delete(cron["cron_id"])
            logger.info(
                "[setup_crons] deleted existing cron %s (%s)",
                cron["cron_id"],
                cron["metadata"]["name"],
            )

    for spec in _CRONS:
        cron = await client.crons.create(
            spec["assistant_id"],
            schedule=spec["schedule"],
            input=spec["input"],
            metadata=spec["metadata"],
        )
        name = spec["metadata"]["name"]
        logger.info(
            "[setup_crons] created %s — cron_id=%s schedule=%s",
            name,
            cron["cron_id"],
            spec["schedule"],
        )

    logger.info("[setup_crons] done — crons are active while the server is running")


if __name__ == "__main__":
    asyncio.run(main())
