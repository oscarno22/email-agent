import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from agent.core.classifier import classify
from agent.core.state import ActionPlan, Category, Features, State, TrustPhase
from agent.stats.db import record_event
from agent.stats.events import publish

logger = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).parent.parent.parent / "logs"

_CATEGORY_ACCENT = {
    "newsletter": "#6b8afd",
    "receipt": "#8e6bfd",
    "calendar": "#fd9b6b",
    "personal": "#3ec48f",
    "work": "#3ec4c4",
    "junk": "#d35d6e",
    "unknown": "#9aa0a6",
}


def _ui_props(state: State, plan: ActionPlan, ts: str) -> dict:
    cat = state.classification.category.value if state.classification else "unknown"
    return {
        "ts": ts,
        "gmail_id": state.email.gmail_id,
        "sender": state.email.sender,
        "subject": state.email.subject,
        "category": cat,
        "confidence": round(state.classification.confidence, 2) if state.classification else 0.0,
        "action_notes": plan.notes,
        "trust_phase": state.trust_phase.value,
        "draft_created": plan.draft_reply is not None,
        "accent_color": _CATEGORY_ACCENT.get(cat, "#9aa0a6"),
    }


def _append_action_log(state: State, plan: ActionPlan) -> dict:
    ts = datetime.now(UTC).isoformat()
    entry = {
        "ts": ts,
        "gmail_id": state.email.gmail_id,
        "sender": state.email.sender,
        "subject": state.email.subject,
        "category": state.classification.category.value if state.classification else "unknown",
        "confidence": round(state.classification.confidence, 2) if state.classification else 0.0,
        "action": plan.notes,
        "trust_phase": state.trust_phase.value,
        "draft_created": plan.draft_reply is not None,
    }
    _LOG_DIR.mkdir(exist_ok=True)
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    with (_LOG_DIR / f"{date_str}.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")

    record_event(
        ts=ts,
        gmail_id=state.email.gmail_id,
        thread_id=state.email.thread_id,
        sender=state.email.sender,
        sender_domain=state.email.sender_domain,
        subject=state.email.subject,
        category=entry["category"],
        confidence=entry["confidence"],
        action_notes=plan.notes,
        trust_phase=state.trust_phase.value,
        draft_created=plan.draft_reply is not None,
    )

    ui_props = _ui_props(state, plan, ts)
    publish({"type": "email_processed", "props": ui_props})

    try:
        from langgraph.graph.ui import push_ui_message

        push_ui_message("email_card", ui_props)
    except Exception:
        # push_ui_message only works inside a LangGraph run context; the
        # smoke script & dashboard invocations don't have one, so swallow.
        logger.debug("[nodes] push_ui_message skipped (no run context)")

    return entry


UNSUBSCRIBE_MARKERS = ("unsubscribe", "manage preferences", "opt out", "opt-out")
KNOWN_PERSONAL_DOMAINS = {"gmail.com", "icloud.com", "hotmail.com", "outlook.com"}


def extract_features(state: State) -> State:
    body_lower = state.email.body.lower()
    features = Features(
        has_unsubscribe=any(m in body_lower for m in UNSUBSCRIBE_MARKERS),
        has_links="http://" in state.email.body or "https://" in state.email.body,
        body_excerpt=state.email.body[:800],
        sender_known=state.email.sender_domain in KNOWN_PERSONAL_DOMAINS,
    )
    return state.model_copy(
        update={
            "features": features,
            "log": [*state.log, f"features: unsubscribe={features.has_unsubscribe}"],
        }
    )


def classify_node(state: State) -> State:
    assert state.features is not None, "extract_features must run before classify"
    classification = classify(
        sender=state.email.sender,
        subject=state.email.subject,
        body_excerpt=state.features.body_excerpt,
    )
    return state.model_copy(
        update={
            "classification": classification,
            "log": [
                *state.log,
                f"classified: {classification.category.value} "
                f"(conf={classification.confidence:.2f})",
            ],
        }
    )


def route_by_category(state: State) -> str:
    assert state.classification is not None
    return state.classification.category.value


def _action(state: State, plan: ActionPlan) -> State:
    if state.trust_phase == TrustPhase.SHADOW:
        plan = plan.model_copy(update={"notes": f"[shadow] would: {plan.notes}"})
    elif state.trust_phase == TrustPhase.LABEL:
        from agent.ingestion.gmail_client import apply_action

        apply_action(state.email.gmail_id, plan.labels_to_apply, plan.archive)
    elif state.trust_phase == TrustPhase.DRAFT:
        from agent.ingestion.gmail_client import apply_action, create_draft

        apply_action(state.email.gmail_id, plan.labels_to_apply, plan.archive)
        if plan.draft_reply:
            create_draft(
                thread_id=state.email.thread_id,
                to=state.email.sender,
                subject=state.email.subject,
                body=plan.draft_reply,
            )
    _append_action_log(state, plan)
    return state.model_copy(
        update={
            "action": plan,
            "log": [*state.log, f"action: {plan.notes}"],
        }
    )


def action_newsletter(state: State) -> State:
    return _action(
        state,
        ActionPlan(
            labels_to_apply=["Newsletters"],
            archive=True,
            notes="label=Newsletters, archive=true",
        ),
    )


def action_receipt(state: State) -> State:
    return _action(
        state,
        ActionPlan(
            labels_to_apply=["Receipts"],
            archive=True,
            notes="label=Receipts, archive=true",
        ),
    )


def action_calendar(state: State) -> State:
    return _action(
        state,
        ActionPlan(
            labels_to_apply=["Calendar"],
            archive=False,
            notes="label=Calendar, keep in inbox",
        ),
    )


def action_personal(state: State) -> State:
    draft_reply = None
    if state.trust_phase == TrustPhase.DRAFT:
        from agent.core.drafter import generate_draft

        draft_reply = generate_draft(state.email)
    return _action(
        state,
        ActionPlan(
            labels_to_apply=["Personal"],
            archive=False,
            draft_reply=draft_reply,
            notes="label=Personal, keep in inbox" + (", draft=true" if draft_reply else ""),
        ),
    )


def action_work(state: State) -> State:
    draft_reply = None
    if state.trust_phase == TrustPhase.DRAFT:
        from agent.core.drafter import generate_draft

        draft_reply = generate_draft(state.email)
    return _action(
        state,
        ActionPlan(
            labels_to_apply=["Work"],
            archive=False,
            draft_reply=draft_reply,
            notes="label=Work, keep in inbox" + (", draft=true" if draft_reply else ""),
        ),
    )


def action_junk(state: State) -> State:
    return _action(
        state,
        ActionPlan(
            labels_to_apply=["Junk"],
            archive=True,
            notes="label=Junk, archive=true",
        ),
    )


def action_unknown(state: State) -> State:
    return _action(
        state,
        ActionPlan(notes="no action — left in inbox for manual review"),
    )


CATEGORY_NODES = {
    Category.NEWSLETTER.value: ("action_newsletter", action_newsletter),
    Category.RECEIPT.value: ("action_receipt", action_receipt),
    Category.CALENDAR.value: ("action_calendar", action_calendar),
    Category.PERSONAL.value: ("action_personal", action_personal),
    Category.WORK.value: ("action_work", action_work),
    Category.JUNK.value: ("action_junk", action_junk),
    Category.UNKNOWN.value: ("action_unknown", action_unknown),
}
