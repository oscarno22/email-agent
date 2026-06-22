"""Tests for graph nodes: extract_features, routing, and action nodes (shadow mode)."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from agent.core.nodes import (
    CATEGORY_NODES,
    action_calendar,
    action_junk,
    action_newsletter,
    action_receipt,
    action_unknown,
    extract_features,
    route_by_category,
)
from agent.core.state import Category, Classification, Email, Features, State, TrustPhase

# ── helpers ──────────────────────────────────────────────────────────────────


def _email(**kwargs) -> Email:
    defaults = {
        "gmail_id": "msg1",
        "thread_id": "thread1",
        "sender": "test@example.com",
        "sender_domain": "example.com",
        "subject": "Test subject",
        "body": "Hello world",
        "received_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    return Email(**defaults)


def _state(
    *, email: Email | None = None, trust_phase: TrustPhase = TrustPhase.SHADOW, **kwargs
) -> State:
    return State(email=email or _email(), trust_phase=trust_phase, **kwargs)


def _classified(category: Category, trust_phase: TrustPhase = TrustPhase.SHADOW) -> State:
    return _state(
        trust_phase=trust_phase,
        features=Features(body_excerpt="hello"),
        classification=Classification(category=category, confidence=0.9, reasoning="test"),
    )


# ── extract_features ─────────────────────────────────────────────────────────


def test_extract_features_unsubscribe_detected():
    state = _state(email=_email(body="Click here to unsubscribe from this list"))
    assert extract_features(state).features.has_unsubscribe is True


def test_extract_features_no_unsubscribe():
    state = _state(email=_email(body="Hey, let's grab lunch soon!"))
    assert extract_features(state).features.has_unsubscribe is False


@pytest.mark.parametrize("marker", ["manage preferences", "opt out", "opt-out"])
def test_extract_features_all_unsubscribe_markers(marker):
    state = _state(email=_email(body=f"Please {marker} here"))
    assert extract_features(state).features.has_unsubscribe is True


def test_extract_features_detects_https_link():
    state = _state(email=_email(body="Visit https://example.com for details"))
    assert extract_features(state).features.has_links is True


def test_extract_features_detects_http_link():
    state = _state(email=_email(body="Visit http://example.com"))
    assert extract_features(state).features.has_links is True


def test_extract_features_no_links():
    state = _state(email=_email(body="No links here at all"))
    assert extract_features(state).features.has_links is False


@pytest.mark.parametrize("domain", ["gmail.com", "icloud.com", "hotmail.com", "outlook.com"])
def test_extract_features_known_personal_domains(domain):
    state = _state(email=_email(sender_domain=domain))
    assert extract_features(state).features.sender_known is True


def test_extract_features_unknown_domain():
    state = _state(email=_email(sender_domain="newsletter.io"))
    assert extract_features(state).features.sender_known is False


def test_extract_features_body_excerpt_capped_at_800():
    state = _state(email=_email(body="x" * 1200))
    assert len(extract_features(state).features.body_excerpt) == 800


def test_extract_features_appends_to_log():
    state = _state()
    result = extract_features(state)
    assert any("features" in entry for entry in result.log)


# ── route_by_category ────────────────────────────────────────────────────────


@pytest.mark.parametrize("cat", list(Category))
def test_route_by_category_returns_category_value(cat):
    state = _classified(cat)
    assert route_by_category(state) == cat.value


def test_route_by_category_all_values_in_category_nodes():
    """Every non-UNKNOWN category must have a node in CATEGORY_NODES."""
    for cat in Category:
        assert cat.value in CATEGORY_NODES, f"Missing node for {cat}"


# ── action nodes (shadow mode — no Gmail API calls) ──────────────────────────


@patch("agent.core.nodes.record_event")
@patch("agent.core.nodes.publish")
def test_action_newsletter_shadow_notes(mock_pub, mock_rec):
    result = action_newsletter(_classified(Category.NEWSLETTER))
    assert "[shadow]" in result.action.notes
    assert "Newsletters" in result.action.notes
    assert result.action.archive is True


@patch("agent.core.nodes.record_event")
@patch("agent.core.nodes.publish")
def test_action_receipt_shadow_stays_in_inbox(mock_pub, mock_rec):
    result = action_receipt(_classified(Category.RECEIPT))
    assert "[shadow]" in result.action.notes
    # Only newsletter and junk archive; receipts stay in the inbox under their label.
    assert result.action.archive is False
    assert "Receipts" in result.action.labels_to_apply


@patch("agent.core.nodes.record_event")
@patch("agent.core.nodes.publish")
def test_action_calendar_shadow_stays_in_inbox(mock_pub, mock_rec):
    result = action_calendar(_classified(Category.CALENDAR))
    assert result.action.archive is False


@patch("agent.core.nodes.record_event")
@patch("agent.core.nodes.publish")
def test_action_junk_shadow_archives(mock_pub, mock_rec):
    result = action_junk(_classified(Category.JUNK))
    assert "[shadow]" in result.action.notes
    assert result.action.archive is True


@patch("agent.core.nodes.record_event")
@patch("agent.core.nodes.publish")
def test_action_unknown_shadow_no_labels(mock_pub, mock_rec):
    result = action_unknown(_classified(Category.UNKNOWN))
    assert result.action.labels_to_apply == []
    assert result.action.archive is False


@patch("agent.core.nodes.record_event")
@patch("agent.core.nodes.publish")
def test_action_records_event(mock_pub, mock_rec):
    action_newsletter(_classified(Category.NEWSLETTER))
    mock_rec.assert_called_once()
    kwargs = mock_rec.call_args.kwargs
    assert kwargs["gmail_id"] == "msg1"
    assert kwargs["category"] == "newsletter"


@patch("agent.core.nodes.record_event")
@patch("agent.core.nodes.publish")
def test_shadow_mode_never_calls_gmail(mock_pub, mock_rec):
    """In SHADOW mode, no gmail_client functions should be imported/called."""
    with patch("agent.ingestion.gmail_client.apply_action") as mock_apply:
        action_newsletter(_classified(Category.NEWSLETTER, TrustPhase.SHADOW))
        mock_apply.assert_not_called()
