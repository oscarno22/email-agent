from langgraph.graph import StateGraph, START, END

from pydantic import BaseModel


class State(BaseModel):
    message: str


def node(state: State) -> State:
    return state


def create_graph():
    graph = StateGraph(State)

    graph.add_node("node", node)

    graph.add_edge(START, "node")
    graph.add_edge("node", END)

    return graph.compile()

graph = create_graph()
