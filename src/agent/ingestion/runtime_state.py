"""In-process runtime health/status, surfaced by `GET /status`.

A tiny thread-safe dict updated from the lifespan, the webhook, the worker, and
the Gmail client wrapper. Lets you see at a glance — in the dashboard or via the
endpoint — whether the Gmail token is valid, when the last push arrived, how deep
the queue is, and when the push watch expires. Pure stdlib so any module
(including `gmail_client`) can import it without a circular dependency.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

_lock = threading.Lock()
_state: dict[str, Any] = {
    "started_at": None,
    "gmail_token_ok": None,  # None until the first Gmail call resolves it
    "last_gmail_error": None,
    "ai_ok": None,  # None until the first classification resolves it
    "last_ai_error": None,
    "last_webhook_at": None,
    "last_email_processed_at": None,
    "watch_expiration": None,  # epoch millis as a string, per Gmail watch API
    "last_renew_status": None,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def update(**fields: Any) -> None:
    with _lock:
        _state.update(fields)


def mark_started() -> None:
    update(started_at=_now())


def mark_gmail_ok() -> None:
    with _lock:
        _state["gmail_token_ok"] = True
        _state["last_gmail_error"] = None


def mark_gmail_error(message: str) -> None:
    with _lock:
        _state["gmail_token_ok"] = False
        _state["last_gmail_error"] = message


def mark_ai_ok() -> None:
    with _lock:
        _state["ai_ok"] = True
        _state["last_ai_error"] = None


def mark_ai_error(message: str) -> None:
    with _lock:
        _state["ai_ok"] = False
        _state["last_ai_error"] = message


def touch_webhook() -> None:
    update(last_webhook_at=_now())


def touch_email_processed() -> None:
    update(last_email_processed_at=_now())


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_state)
