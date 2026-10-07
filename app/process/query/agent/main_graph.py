from langgraph.graph import StateGraph, END
from langgraph.types import Send

from app.process.query.agent.nodes.node_answer_output import node_answer_output
from app.process.query.agent.nodes.node_item_name_confirm import node_item_name_confirm
from app.process.query.agent.nodes.node_rerank import node_rerank
from app.process.query.agent.nodes.node_rrf import node_rrf
from app.process.query.agent.nodes.node_search_embedding import node_search_embedding
from app.process.query.agent.nodes.node_search_embedding_hyde import node_search_embedding_hyde
from app.process.query.agent.nodes.node_web_search_mcp import node_web_search_mcp
from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.logger import logger

builder = StateGraph(QueryGraphState)

builder.add_node(node_item_name_confirm)
builder.add_node(node_search_embedding)
builder.add_node(node_search_embedding_hyde)
builder.add_node(node_web_search_mcp)
builder.add_node(node_rrf)
builder.add_node(node_rerank)
builder.add_node(node_answer_output)

builder.set_entry_point("node_item_name_confirm")

def router(state: QueryGraphState) -> str | list[Send]:
    if state.get("answer"):
        return "node_answer_output"

    return [
        Send("node_search_embedding", state),
        Send("node_search_embedding_hyde", state),
        Send("node_web_search_mcp", state)
    ]

builder.add_conditional_edges(  
    "node_item_name_confirm", 
    router, 
    path_map={
        "node_answer_output": "node_answer_output",
        "node_search_embedding": "node_search_embedding",
        "node_search_embedding_hyde": "node_search_embedding_hyde",
        "node_web_search_mcp": "node_web_search_mcp"
    }                       
)

builder.add_edge("node_search_embedding", "node_rrf")
builder.add_edge("node_search_embedding_hyde", "node_rrf")
builder.add_edge("node_web_search_mcp", "node_rrf")
builder.add_edge("node_rrf", "node_rerank")
builder.add_edge("node_rerank", "node_answer_output")
builder.add_edge("node_answer_output", END)

query_graph = builder.compile()
