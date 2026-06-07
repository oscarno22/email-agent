"""
Register LangGraph cron jobs for watch renewal and morning digest.

Run once after the server starts:
    make setup-crons

Safe to re-run — existing crons with the same metadata tag are deleted first.
"""

import asyncio
import os

from langgraph_sdk import get_client

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
    client = get_client(url=_URL)

    # Remove any previously registered managed crons so re-runs are idempotent.
    existing = await client.crons.search(limit=100)
    for cron in existing:
        if cron.get("metadata", {}).get("managed_by") == "setup_crons":
            await client.crons.delete(cron["cron_id"])
            print(f"  deleted existing cron {cron['cron_id']} ({cron['metadata']['name']})")

    for spec in _CRONS:
        cron = await client.crons.create(
            spec["assistant_id"],
            schedule=spec["schedule"],
            input=spec["input"],
            metadata=spec["metadata"],
        )
        print(f"  created {spec['metadata']['name']} — cron_id={cron['cron_id']}  schedule={spec['schedule']}")

    print("\nDone. Crons are active while the server is running.")


if __name__ == "__main__":
    asyncio.run(main())
