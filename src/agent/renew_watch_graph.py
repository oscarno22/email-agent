import os
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent.gmail_client import register_watch

TOPIC = os.getenv("PUBSUB_TOPIC_NAME", "projects/email-agent-ozzy/topics/gmail-push")


class WatchState(TypedDict, total=False):
    history_id: str
    expiration: str


def renew(state: WatchState) -> WatchState:
    result = register_watch(TOPIC)
    return {"history_id": str(result["historyId"]), "expiration": str(result["expiration"])}


_builder = StateGraph(WatchState)
_builder.add_node("renew", renew)
_builder.add_edge(START, "renew")
_builder.add_edge("renew", END)
graph = _builder.compile()
