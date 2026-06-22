import logging

from agent.core.llm import LLMError, Tier, get_provider
from agent.core.rules import DEFAULT_RULES
from agent.core.state import Category, Classification

logger = logging.getLogger(__name__)

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

CONFIDENCE_ESCALATION_THRESHOLD = 0.6

CLASSIFY_TOOL = {
    "name": "classify_email",
    "description": "Record the category for an incoming email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": [c.value for c in Category if c is not Category.UNKNOWN],
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0..1 confidence in this category.",
            },
            "reasoning": {
                "type": "string",
                "description": "One-sentence reason citing the rule or signal used.",
            },
        },
        "required": ["category", "confidence", "reasoning"],
    },
}

SYSTEM_PROMPT = """You triage incoming Gmail messages into a fixed set of categories.

You will be given a set of user-editable rules and a single email (sender, subject, body
excerpt). Apply the rules first; fall back to general judgment only when rules don't
clearly apply. Cite the rule or signal you used in the reasoning field.

Always call the classify_email tool — never reply in prose."""


def _build_user_message(rules: str, sender: str, subject: str, body_excerpt: str) -> str:
    return f"""<rules>
{rules}
</rules>

<email>
From: {sender}
Subject: {subject}

{body_excerpt}
</email>"""


def classify(
    sender: str,
    subject: str,
    body_excerpt: str,
    rules: str = DEFAULT_RULES,
) -> Classification:
    provider = get_provider()
    user_msg = _build_user_message(rules, sender, subject, body_excerpt)

    def _call(tier: Tier) -> Classification:
        try:
            data = provider.structured_completion(
                tier=tier,
                system=SYSTEM_PROMPT,
                user=user_msg,
                tool_name=CLASSIFY_TOOL["name"],
                tool_description=CLASSIFY_TOOL["description"],
                tool_schema=CLASSIFY_TOOL["input_schema"],
            )
        except LLMError as exc:
            logger.error("[classify] LLM call failed (tier=%s): %s", tier.value, exc)
            raise
        result = Classification(
            category=Category(data["category"]),
            confidence=float(data["confidence"]),
            reasoning=data["reasoning"],
        )
        logger.info(
            "[classify] tier=%s category=%s confidence=%.2f",
            tier.value,
            result.category.value,
            result.confidence,
        )
        return result

    first = _call(Tier.FAST)
    if first.confidence >= CONFIDENCE_ESCALATION_THRESHOLD:
        return first

    logger.info(
        "[classify] confidence %.2f < %.2f — escalating to DEEP tier",
        first.confidence,
        CONFIDENCE_ESCALATION_THRESHOLD,
    )
    escalated = _call(Tier.DEEP)
    escalated.needs_escalation = True
    return escalated


__all__ = ["classify", "CLASSIFY_TOOL", "HAIKU", "SONNET"]
