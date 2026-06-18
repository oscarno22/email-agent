"""Tests for the two-tier classifier (Haiku → Sonnet escalation)."""

from unittest.mock import MagicMock

import pytest

from agent.core.classifier import (
    CONFIDENCE_ESCALATION_THRESHOLD,
    HAIKU,
    SONNET,
    _build_user_message,
    _parse_tool_use,
    classify,
)
from agent.core.state import Category


def _tool_response(category: str, confidence: float, reasoning: str = "test reason") -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "classify_email"
    block.input = {"category": category, "confidence": confidence, "reasoning": reasoning}
    response = MagicMock()
    response.content = [block]
    return response


def _mock_client(*responses) -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = list(responses)
    return client


# ── _build_user_message ──────────────────────────────────────────────────────


def test_build_user_message_contains_sender_and_subject():
    msg = _build_user_message("rules", "alice@example.com", "Hello", "body text")
    assert "alice@example.com" in msg
    assert "Hello" in msg
    assert "body text" in msg
    assert "rules" in msg


# ── _parse_tool_use ──────────────────────────────────────────────────────────


def test_parse_tool_use_extracts_classification():
    resp = _tool_response("newsletter", 0.9, "matched Substack rule")
    result = _parse_tool_use(resp)
    assert result.category == Category.NEWSLETTER
    assert result.confidence == 0.9
    assert result.reasoning == "matched Substack rule"


def test_parse_tool_use_raises_when_no_tool_called():
    block = MagicMock()
    block.type = "text"
    resp = MagicMock()
    resp.content = [block]
    with pytest.raises(ValueError, match="classify_email"):
        _parse_tool_use(resp)


def test_parse_tool_use_skips_non_classify_tool():
    other = MagicMock()
    other.type = "tool_use"
    other.name = "some_other_tool"
    resp = MagicMock()
    resp.content = [other]
    with pytest.raises(ValueError, match="classify_email"):
        _parse_tool_use(resp)


# ── classify — escalation behaviour ─────────────────────────────────────────


def test_high_confidence_uses_haiku_only():
    client = _mock_client(_tool_response("newsletter", 0.95))
    result = classify("news@substack.com", "Weekly digest", "body", client=client)
    assert result.category == Category.NEWSLETTER
    assert result.confidence == 0.95
    assert not result.needs_escalation
    assert client.messages.create.call_count == 1
    assert client.messages.create.call_args.kwargs["model"] == HAIKU


def test_low_confidence_escalates_to_sonnet():
    client = _mock_client(
        _tool_response("newsletter", 0.4),  # Haiku: uncertain
        _tool_response("work", 0.88),  # Sonnet: confident
    )
    result = classify("boss@company.com", "Q3 review", "body", client=client)
    assert result.category == Category.WORK
    assert result.needs_escalation is True
    assert client.messages.create.call_count == 2
    calls = client.messages.create.call_args_list
    assert calls[0].kwargs["model"] == HAIKU
    assert calls[1].kwargs["model"] == SONNET


def test_confidence_at_threshold_does_not_escalate():
    client = _mock_client(_tool_response("receipt", CONFIDENCE_ESCALATION_THRESHOLD))
    result = classify("stripe@stripe.com", "Your receipt", "body", client=client)
    assert result.category == Category.RECEIPT
    assert not result.needs_escalation
    assert client.messages.create.call_count == 1


def test_custom_rules_forwarded_to_model():
    client = _mock_client(_tool_response("junk", 0.9))
    classify("x@x.com", "hi", "body", rules="custom rules here", client=client)
    call_msg = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "custom rules here" in call_msg


def test_all_non_unknown_categories_accepted():
    """Every category the model is allowed to return round-trips correctly."""
    for cat in Category:
        if cat is Category.UNKNOWN:
            continue
        client = _mock_client(_tool_response(cat.value, 0.9))
        result = classify("a@b.com", "s", "b", client=client)
        assert result.category == cat
