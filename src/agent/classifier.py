import os

from anthropic import Anthropic

from agent.rules import DEFAULT_RULES
from agent.state import Category, Classification

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


def _parse_tool_use(response) -> Classification:
    for block in response.content:
        if block.type == "tool_use" and block.name == "classify_email":
            data = block.input
            return Classification(
                category=Category(data["category"]),
                confidence=float(data["confidence"]),
                reasoning=data["reasoning"],
            )
    raise ValueError(f"Model did not call classify_email: {response.content!r}")


def classify(
    sender: str,
    subject: str,
    body_excerpt: str,
    rules: str = DEFAULT_RULES,
    client: Anthropic | None = None,
) -> Classification:
    client = client or Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def _call(model: str) -> Classification:
        response = client.messages.create(
            model=model,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            tools=[CLASSIFY_TOOL],
            tool_choice={"type": "tool", "name": "classify_email"},
            messages=[
                {
                    "role": "user",
                    "content": _build_user_message(rules, sender, subject, body_excerpt),
                }
            ],
        )
        return _parse_tool_use(response)

    first = _call(HAIKU)
    if first.confidence >= CONFIDENCE_ESCALATION_THRESHOLD:
        return first

    escalated = _call(SONNET)
    escalated.needs_escalation = True
    return escalated


__all__ = ["classify", "CLASSIFY_TOOL", "HAIKU", "SONNET"]
