from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent.digest import main as run_digest


class DigestState(TypedDict, total=False):
    sent: bool


def send_digest(state: DigestState) -> DigestState:
    run_digest()
    return {"sent": True}


_builder = StateGraph(DigestState)
_builder.add_node("digest", send_digest)
_builder.add_edge(START, "digest")
_builder.add_edge("digest", END)
graph = _builder.compile()
