"""Regenerate a Gmail OAuth refresh token.

Run when `GMAIL_REFRESH_TOKEN` has expired (e.g. an OAuth consent screen in
"Testing" mode revokes refresh tokens after 7 days — publish it to "In
production" to make tokens durable) or when first setting the agent up.

Builds the OAuth client config in-memory from `GMAIL_CLIENT_ID` /
`GMAIL_CLIENT_SECRET` env vars (no `credentials.json` on disk) and runs the
installed-app flow in a local browser. Prints the resulting refresh token to
paste into `src/.env` (GMAIL_REFRESH_TOKEN) and `cloudformation/secrets.json`.

    make refresh-token
"""

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

from agent.ingestion.gmail_client import SCOPES


def main() -> None:
    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")
    if not (client_id and client_secret):
        sys.exit(
            "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in src/.env "
            "(run via `make refresh-token`, which sources it)."
        )

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # offline + consent guarantees a refresh token is returned every run.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )

    if not creds.refresh_token:
        sys.exit("No refresh token returned. Re-run and ensure you grant consent.")

    print("\n" + "=" * 70)
    print("GMAIL_REFRESH_TOKEN obtained. Paste into src/.env AND")
    print("cloudformation/secrets.json, then `make secrets-create` to deploy.\n")
    print(creds.refresh_token)
    print("=" * 70)


if __name__ == "__main__":
    main()
