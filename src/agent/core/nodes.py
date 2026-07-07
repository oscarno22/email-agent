import os
from datetime import UTC, datetime

from agent.core.classifier import classify
from agent.core.state import ActionPlan, Category, Email, Features, State, TrustPhase
from agent.stats.db import record_event
from agent.stats.events import publish

# `or` (not getenv's default arg) so an env var that is present-but-empty — as it is
# in production when the CloudFormation DigestToEmail param is unset — still falls back.
_DIGEST_TO = os.getenv("DIGEST_TO_EMAIL") or "oscarnolen@gmail.com"

# Category labels are top-level (no parent prefix) so they coexist with — and reuse —
# labels you already manage by hand (e.g. "Banking", "Applications/Assessments") instead
# of the agent creating a duplicate nested copy. Only the digests and the agent's own
# operational labels (Needs Review, Alerts) stay under the "Email Agent/" parent.
_LABEL_PREFIX = ""


def is_own_digest(email: Email) -> bool:
    """The agent's own digest emails (sent self->self).

    Never draft a reply to these; the webhook worker also skips classifying them
    and just archives them on arrival (the send-time archive can race delivery).
    """
    return email.sender.strip().lower() == _DIGEST_TO.strip().lower() and email.subject.startswith(
        "Email Agent"
    )


_CATEGORY_ACCENT = {
    "newsletter": "#6b8afd",
    "receipt": "#8e6bfd",
    "calendar": "#fd9b6b",
    "personal": "#3ec48f",
    "work": "#3ec4c4",
    "banking": "#c9a227",
    "application": "#5b8def",
    "assessment": "#a76bfd",
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


def _append_action_log(state: State, plan: ActionPlan) -> None:
    ts = datetime.now(UTC).isoformat()
    cat = state.classification.category.value if state.classification else "unknown"
    confidence = round(state.classification.confidence, 2) if state.classification else 0.0
    record_event(
        ts=ts,
        gmail_id=state.email.gmail_id,
        thread_id=state.email.thread_id,
        sender=state.email.sender,
        sender_domain=state.email.sender_domain,
        subject=state.email.subject,
        category=cat,
        confidence=confidence,
        action_notes=plan.notes,
        trust_phase=state.trust_phase.value,
        draft_created=plan.draft_reply is not None,
    )
    ui_props = _ui_props(state, plan, ts)
    publish({"type": "email_processed", "props": ui_props})


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
    from agent.core.rules import DEFAULT_RULES
    from agent.stats.db import get_user_rules

    user_rule_rows = get_user_rules()
    if user_rule_rows:
        user_rule_text = "\n".join(r["rule"] for r in user_rule_rows)
        rules = f"{user_rule_text}\n{DEFAULT_RULES}"
    else:
        rules = DEFAULT_RULES

    classification = classify(
        sender=state.email.sender,
        subject=state.email.subject,
        body_excerpt=state.features.body_excerpt,
        rules=rules,
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
    phase = state.trust_phase
    if phase == TrustPhase.SHADOW:
        # Observe only — never touch Gmail; record what we *would* have done.
        plan = plan.model_copy(update={"notes": f"[shadow] would: {plan.notes}"})
    elif phase == TrustPhase.LABEL:
        # Labels only — force the message to stay in the inbox so nothing
        # disappears while we're still building trust in the classifier.
        from agent.ingestion.gmail_client import apply_action

        apply_action(state.email.gmail_id, plan.labels_to_apply, archive=False)
    elif phase in (TrustPhase.ARCHIVE, TrustPhase.DRAFT):
        # Trusted to declutter: apply labels and honor the plan's archive
        # decision. DRAFT additionally writes a reply draft when one was planned.
        from agent.ingestion.gmail_client import apply_action

        apply_action(state.email.gmail_id, plan.labels_to_apply, plan.archive)
        if phase == TrustPhase.DRAFT and plan.draft_reply:
            from agent.ingestion.gmail_client import create_draft

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
            labels_to_apply=[f"{_LABEL_PREFIX}Newsletters"],
            archive=True,
            notes="label=Newsletters, archive=true",
        ),
    )


def action_receipt(state: State) -> State:
    return _action(
        state,
        ActionPlan(
            labels_to_apply=[f"{_LABEL_PREFIX}Receipts"],
            archive=False,
            notes="label=Receipts, keep in inbox",
        ),
    )


def action_calendar(state: State) -> State:
    return _action(
        state,
        ActionPlan(
            labels_to_apply=[f"{_LABEL_PREFIX}Calendar"],
            archive=False,
            notes="label=Calendar, keep in inbox",
        ),
    )


def action_personal(state: State) -> State:
    draft_reply = None
    if state.trust_phase == TrustPhase.DRAFT and not is_own_digest(state.email):
        from agent.core.drafter import generate_draft

        draft_reply = generate_draft(state.email)
    return _action(
        state,
        ActionPlan(
            labels_to_apply=[f"{_LABEL_PREFIX}Personal"],
            archive=False,
            draft_reply=draft_reply,
            notes="label=Personal, keep in inbox" + (", draft=true" if draft_reply else ""),
        ),
    )


def action_work(state: State) -> State:
    draft_reply = None
    if state.trust_phase == TrustPhase.DRAFT and not is_own_digest(state.email):
        from agent.core.drafter import generate_draft

        draft_reply = generate_draft(state.email)
    return _action(
        state,
        ActionPlan(
            labels_to_apply=[f"{_LABEL_PREFIX}Work"],
            archive=False,
            draft_reply=draft_reply,
            notes="label=Work, keep in inbox" + (", draft=true" if draft_reply else ""),
        ),
    )


def action_banking(state: State) -> State:
    return _action(
        state,
        ActionPlan(
            labels_to_apply=[f"{_LABEL_PREFIX}Banking"],
            archive=False,
            notes="label=Banking, keep in inbox",
        ),
    )


def action_application(state: State) -> State:
    draft_reply = None
    if state.trust_phase == TrustPhase.DRAFT and not is_own_digest(state.email):
        from agent.core.drafter import generate_draft

        draft_reply = generate_draft(state.email)
    return _action(
        state,
        ActionPlan(
            labels_to_apply=[f"{_LABEL_PREFIX}Applications"],
            archive=False,
            draft_reply=draft_reply,
            notes="label=Applications, keep in inbox" + (", draft=true" if draft_reply else ""),
        ),
    )


def action_assessment(state: State) -> State:
    return _action(
        state,
        ActionPlan(
            labels_to_apply=[f"{_LABEL_PREFIX}Applications/Assessments"],
            archive=False,
            notes="label=Applications/Assessments, keep in inbox",
        ),
    )


def action_junk(state: State) -> State:
    return _action(
        state,
        ActionPlan(
            labels_to_apply=[f"{_LABEL_PREFIX}Junk"],
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
    Category.BANKING.value: ("action_banking", action_banking),
    Category.APPLICATION.value: ("action_application", action_application),
    Category.ASSESSMENT.value: ("action_assessment", action_assessment),
    Category.JUNK.value: ("action_junk", action_junk),
    Category.UNKNOWN.value: ("action_unknown", action_unknown),
}
