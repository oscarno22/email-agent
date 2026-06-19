"""Process-local broadcast bus that the dashboard's SSE endpoint subscribes to.

Action nodes call publish() after recording a stats row; any number of SSE
clients receive a fan-out copy via subscribe(). Graph nodes run in a thread
pool (sync callables under ainvoke), so publish() hops back to the event loop
via call_soon_threadsafe. Best-effort — if no loop is attached (e.g. the
smoke script or `make dashboard` standalone), publish is a no-op.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
_loop: asyncio.AbstractEventLoop | None = None


def attach_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once at dashboard startup so cross-thread publish can hop back here."""
    global _loop
    _loop = loop


def publish(event: dict[str, Any]) -> None:
    if _loop is None or _loop.is_closed():
        return
    # Loop may be dying — drop the event in that case.
    with contextlib.suppress(RuntimeError):
        _loop.call_soon_threadsafe(_fanout, event)


def _fanout(event: dict[str, Any]) -> None:
    dead: list[asyncio.Queue[dict[str, Any]]] = []
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.discard(q)


async def subscribe() -> AsyncIterator[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
    _subscribers.add(q)
    try:
        while True:
            yield await q.get()
    finally:
        _subscribers.discard(q)
