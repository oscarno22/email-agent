"""Tests for the two-tier classifier (FAST → DEEP escalation)."""

from unittest.mock import MagicMock, patch

import pytest

from agent.core.classifier import (
    CONFIDENCE_ESCALATION_THRESHOLD,
    _build_user_message,
    classify,
)
from agent.core.llm import Tier
from agent.core.state import Category


def _mock_provider(*tool_outputs: dict) -> MagicMock:
    provider = MagicMock()
    provider.structured_completion.side_effect = list(tool_outputs)
    return provider


def _tool_output(category: str, confidence: float, reasoning: str = "test reason") -> dict:
    return {"category": category, "confidence": confidence, "reasoning": reasoning}


# ── _build_user_message ──────────────────────────────────────────────────────


def test_build_user_message_contains_sender_and_subject():
    msg = _build_user_message("rules", "alice@example.com", "Hello", "body text")
    assert "alice@example.com" in msg
    assert "Hello" in msg
    assert "body text" in msg
    assert "rules" in msg


# ── classify — escalation behaviour ─────────────────────────────────────────


def test_high_confidence_uses_fast_only():
    provider = _mock_provider(_tool_output("newsletter", 0.95))
    with patch("agent.core.classifier.get_provider", return_value=provider):
        result = classify("news@substack.com", "Weekly digest", "body")
    assert result.category == Category.NEWSLETTER
    assert result.confidence == 0.95
    assert not result.needs_escalation
    assert provider.structured_completion.call_count == 1
    assert provider.structured_completion.call_args.kwargs["tier"] == Tier.FAST


def test_low_confidence_escalates_to_deep():
    provider = _mock_provider(
        _tool_output("newsletter", 0.4),  # FAST: uncertain
        _tool_output("work", 0.88),  # DEEP: confident
    )
    with patch("agent.core.classifier.get_provider", return_value=provider):
        result = classify("boss@company.com", "Q3 review", "body")
    assert result.category == Category.WORK
    assert result.needs_escalation is True
    assert provider.structured_completion.call_count == 2
    calls = provider.structured_completion.call_args_list
    assert calls[0].kwargs["tier"] == Tier.FAST
    assert calls[1].kwargs["tier"] == Tier.DEEP


def test_confidence_at_threshold_does_not_escalate():
    provider = _mock_provider(_tool_output("receipt", CONFIDENCE_ESCALATION_THRESHOLD))
    with patch("agent.core.classifier.get_provider", return_value=provider):
        result = classify("stripe@stripe.com", "Your receipt", "body")
    assert result.category == Category.RECEIPT
    assert not result.needs_escalation
    assert provider.structured_completion.call_count == 1


def test_custom_rules_forwarded_to_model():
    provider = _mock_provider(_tool_output("junk", 0.9))
    with patch("agent.core.classifier.get_provider", return_value=provider):
        classify("x@x.com", "hi", "body", rules="custom rules here")
    user_msg = provider.structured_completion.call_args.kwargs["user"]
    assert "custom rules here" in user_msg


@pytest.mark.parametrize("cat", [c for c in Category if c is not Category.UNKNOWN])
def test_all_non_unknown_categories_accepted(cat):
    """Every category the model is allowed to return round-trips correctly."""
    provider = _mock_provider(_tool_output(cat.value, 0.9))
    with patch("agent.core.classifier.get_provider", return_value=provider):
        result = classify("a@b.com", "s", "b")
    assert result.category == cat
