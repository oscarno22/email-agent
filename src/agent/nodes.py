import json
from datetime import UTC, datetime
from pathlib import Path

from agent.classifier import classify
from agent.state import ActionPlan, Category, Features, State, TrustPhase

_LOG_DIR = Path(__file__).parent.parent / "logs"


def _append_action_log(state: State, plan: ActionPlan) -> None:
    _LOG_DIR.mkdir(exist_ok=True)
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "gmail_id": state.email.gmail_id,
        "sender": state.email.sender,
        "subject": state.email.subject,
        "category": state.classification.category.value if state.classification else "unknown",
        "confidence": round(state.classification.confidence, 2) if state.classification else 0.0,
        "action": plan.notes,
        "trust_phase": state.trust_phase.value,
    }
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    with (_LOG_DIR / f"{date_str}.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")


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
        from agent.gmail_client import apply_action

        apply_action(state.email.gmail_id, plan.labels_to_apply, plan.archive)
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
    return _action(
        state,
        ActionPlan(
            labels_to_apply=["Personal"],
            archive=False,
            notes="label=Personal, keep in inbox",
        ),
    )


def action_work(state: State) -> State:
    return _action(
        state,
        ActionPlan(
            labels_to_apply=["Work"],
            archive=False,
            notes="label=Work, keep in inbox",
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
