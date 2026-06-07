"""Run the graph against fixture emails. Requires ANTHROPIC_API_KEY in env.

uv run python -m agent.smoke
"""

from agent.core.graph import graph
from agent.core.state import State
from agent.dev.fixtures import ALL


def main() -> None:
    for email in ALL:
        result = graph.invoke(State(email=email))
        cls = result["classification"]
        action = result["action"]
        print(f"\n=== {email.sender_domain} | {email.subject[:60]}")
        print(f"  category: {cls.category.value} (conf={cls.confidence:.2f})")
        print(f"  reason:   {cls.reasoning}")
        print(f"  action:   {action.notes}")
        if cls.needs_escalation:
            print("  ↑ escalated to sonnet")


if __name__ == "__main__":
    main()
