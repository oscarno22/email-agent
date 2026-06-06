from langgraph.graph import END, START, StateGraph

from agent.nodes import (
    CATEGORY_NODES,
    classify_node,
    extract_features,
    route_by_category,
)
from agent.state import State


def create_graph():
    graph = StateGraph(State)

    graph.add_node("extract_features", extract_features)
    graph.add_node("classify", classify_node)
    for node_name, node_fn in CATEGORY_NODES.values():
        graph.add_node(node_name, node_fn)

    graph.add_edge(START, "extract_features")
    graph.add_edge("extract_features", "classify")

    graph.add_conditional_edges(
        "classify",
        route_by_category,
        {category: node_name for category, (node_name, _) in CATEGORY_NODES.items()},
    )

    for node_name, _ in CATEGORY_NODES.values():
        graph.add_edge(node_name, END)

    return graph.compile()


graph = create_graph()
