"""HTTP Basic Auth for the dashboard routes.

The dashboard is mounted into the public webapp (reachable at the ngrok URL),
so its routes — including the inbox-mutating `/api/batch-review` and
`/api/rules` endpoints — must not be open. This module exposes a single
FastAPI dependency, `require_dashboard_auth`, applied to the dashboard router.

Like the webhook's `PUBSUB_VERIFICATION_TOKEN` check, auth is **fail-open when
unconfigured**: if `DASHBOARD_PASSWORD` is unset (local `make start` /
`make dashboard`) the dependency is a no-op. In any deployment the password is
supplied via Secrets Manager, so auth is enforced there.
"""

import logging
import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger(__name__)

_security = HTTPBasic(auto_error=False)


def require_dashboard_auth(
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> None:
    """Enforce HTTP Basic Auth on dashboard routes when a password is configured."""
    expected_password = os.getenv("DASHBOARD_PASSWORD", "")
    if not expected_password:
        # Unconfigured → open (local dev convenience).
        return

    expected_user = os.getenv("DASHBOARD_USER", "admin")
    if credentials is not None:
        user_ok = secrets.compare_digest(credentials.username, expected_user)
        pass_ok = secrets.compare_digest(credentials.password, expected_password)
        if user_ok and pass_ok:
            return

    logger.warning("[auth] dashboard request rejected — missing/invalid credentials")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )
