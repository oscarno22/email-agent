from datetime import datetime

from agent.state import Email

NEWSLETTER = Email(
    gmail_id="fix-1",
    thread_id="t-1",
    sender="Stratechery <newsletters@stratechery.com>",
    sender_domain="stratechery.com",
    subject="Stratechery Daily Update — The State of AI Agents",
    body=(
        "Hi there, here is today's update. ... read more at https://stratechery.com/2026/...\n\n"
        "Unsubscribe | Manage preferences | Forward to a friend"
    ),
    received_at=datetime(2026, 6, 6, 9, 0),
)

RECEIPT = Email(
    gmail_id="fix-2",
    thread_id="t-2",
    sender="Stripe <receipts@stripe.com>",
    sender_domain="stripe.com",
    subject="Your receipt from Anthropic [#2026-06-06-abc]",
    body=("Thanks for your purchase. Amount: $20.00. View invoice: https://stripe.com/...\n"),
    received_at=datetime(2026, 6, 6, 10, 15),
)

CALENDAR = Email(
    gmail_id="fix-3",
    thread_id="t-3",
    sender="Google Calendar <calendar-notification@google.com>",
    sender_domain="google.com",
    subject="Invitation: Coffee with Sam @ Fri Jun 12, 2026 10am",
    body="You have been invited to the following event. Coffee with Sam. When: Fri Jun 12.",
    received_at=datetime(2026, 6, 6, 11, 0),
)

PERSONAL = Email(
    gmail_id="fix-4",
    thread_id="t-4",
    sender="Mom <mom@gmail.com>",
    sender_domain="gmail.com",
    subject="dinner sunday?",
    body="hey, are you free for dinner this sunday? dad is making lasagna. xo",
    received_at=datetime(2026, 6, 6, 12, 30),
)

WORK = Email(
    gmail_id="fix-5",
    thread_id="t-5",
    sender="Priya <priya@acme.co>",
    sender_domain="acme.co",
    subject="Re: Q3 planning doc — comments",
    body=(
        "I left a few comments on the planning doc — mostly around the timeline for the "
        "ingest rewrite. Can we sync tomorrow?"
    ),
    received_at=datetime(2026, 6, 6, 14, 5),
)

JUNK = Email(
    gmail_id="fix-6",
    thread_id="t-6",
    sender="Tyler @ NoNameCo <tyler@nonameco.io>",
    sender_domain="nonameco.io",
    subject="Quick chat about a senior eng role?",
    body=(
        "Hi! I came across your profile and think you'd be a great fit for a senior eng "
        "role at NoNameCo, a seed-stage startup in stealth. Are you open to chatting?"
    ),
    received_at=datetime(2026, 6, 6, 15, 30),
)

ALL = [NEWSLETTER, RECEIPT, CALENDAR, PERSONAL, WORK, JUNK]
