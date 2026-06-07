import base64
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from agent.state import Email

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

_SRC_DIR = Path(__file__).parent.parent  # src/agent/../ = src/
CREDENTIALS_PATH = _SRC_DIR / "credentials.json"
TOKEN_PATH = _SRC_DIR / "token.json"

_service = None
_label_id_cache: dict[str, str] = {}


def get_service():
    global _service
    if _service is not None:
        return _service

    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")
    refresh_token = os.getenv("GMAIL_REFRESH_TOKEN")

    if client_id and client_secret and refresh_token:
        # Env-var path: works in Docker without credential files.
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        creds.refresh(Request())
        logger.debug("[gmail] credentials loaded from env vars")
    else:
        # File-based path: used in local dev with credentials.json / token.json.
        creds = None
        if TOKEN_PATH.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
                creds = flow.run_local_server(port=0)
            TOKEN_PATH.write_text(creds.to_json())
        logger.debug("[gmail] credentials loaded from token.json")

    _service = build("gmail", "v1", credentials=creds)
    return _service


def list_history(start_history_id: str) -> list[str]:
    """Return message IDs for new INBOX messages since startHistoryId."""
    logger.debug("[gmail] history.list(startHistoryId=%s)", start_history_id)
    result = (
        get_service()
        .users()
        .history()
        .list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
        )
        .execute()
    )
    ids = []
    for record in result.get("history", []):
        for added in record.get("messagesAdded", []):
            msg = added["message"]
            if "INBOX" in msg.get("labelIds", []):
                ids.append(msg["id"])
    logger.debug("[gmail] history.list returned %d record(s), %d INBOX message(s)", len(result.get("history", [])), len(ids))
    return ids


def _extract_plain_text(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            padding = "=" * (-len(data) % 4)
            return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text = _extract_plain_text(part)
        if text:
            return text
    return ""


def fetch_email(message_id: str) -> Email:
    """Fetch a Gmail message and map it to an Email."""
    logger.debug("[gmail] fetching message %s", message_id)
    msg = (
        get_service()
        .users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    raw_from = headers.get("from", "")
    if "<" in raw_from and ">" in raw_from:
        sender = raw_from[raw_from.index("<") + 1 : raw_from.index(">")]
    else:
        sender = raw_from.strip()
    sender_domain = sender.split("@")[-1] if "@" in sender else ""
    received_at = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
    body = _extract_plain_text(msg["payload"])
    return Email(
        gmail_id=message_id,
        thread_id=msg["threadId"],
        sender=sender,
        sender_domain=sender_domain,
        subject=headers.get("subject", "(no subject)"),
        body=body[:4000],
        received_at=received_at,
    )


def _get_label_id(name: str) -> str:
    if name in _label_id_cache:
        return _label_id_cache[name]
    labels = get_service().users().labels().list(userId="me").execute().get("labels", [])
    by_name = {label["name"]: label["id"] for label in labels}
    if name not in by_name:
        created = (
            get_service()
            .users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        by_name[name] = created["id"]
    _label_id_cache[name] = by_name[name]
    return _label_id_cache[name]


def apply_action(message_id: str, labels_to_apply: list[str], archive: bool) -> None:
    """Apply labels and optionally remove from INBOX."""
    if not labels_to_apply and not archive:
        logger.info("[gmail] apply_action message=%s — no-op, skipping", message_id)
        return
    logger.info("[gmail] apply_action message=%s labels=%s archive=%s", message_id, labels_to_apply, archive)
    add_ids = [_get_label_id(name) for name in labels_to_apply]
    remove_ids = ["INBOX"] if archive else []
    get_service().users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": add_ids, "removeLabelIds": remove_ids},
    ).execute()


def register_watch(topic_name: str) -> dict:
    """Register Gmail push notifications for INBOX changes. Must be renewed every 7 days."""
    return (
        get_service()
        .users()
        .watch(
            userId="me",
            body={
                "topicName": topic_name,
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "INCLUDE",
            },
        )
        .execute()
    )
