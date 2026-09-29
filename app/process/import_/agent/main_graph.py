from dotenv import load_dotenv
from langgraph.graph import StateGraph, END

from app.process.import_.agent.state import ImportGraphState
from app.process.import_.agent.nodes.node_entry import node_entry
from app.process.import_.agent.nodes.node_pdf_to_md import node_pdf_to_md
from app.process.import_.agent.nodes.node_md_img import node_md_img
from app.process.import_.agent.nodes.node_document_split import node_document_split
from app.process.import_.agent.nodes.node_item_name_recognition import node_item_name_recognition
from app.process.import_.agent.nodes.node_bge_embedding import node_bge_embedding
from app.process.import_.agent.nodes.node_import_milvus import node_import_milvus

load_dotenv()

import_builder = StateGraph(state_schema=ImportGraphState)

import_builder.add_node(node_entry)
import_builder.add_node(node_pdf_to_md)
import_builder.add_node(node_md_img)
import_builder.add_node(node_document_split)
import_builder.add_node(node_item_name_recognition)
import_builder.add_node(node_bge_embedding)
import_builder.add_node(node_import_milvus)

import_builder.set_entry_point("node_entry")

def router(state: ImportGraphState) -> str:
    if state["is_pdf_read_enabled"]:
        return "node_pdf_to_md"
    elif state["is_md_read_enabled"]:
        return "node_md_img"
    else:
        return END

import_builder.add_conditional_edges(
    source="node_entry",
    path=router,
    path_map={
        "node_pdf_to_md": "node_pdf_to_md",
        "node_md_img": "node_md_img",
        END: END
    }
)

import_builder.add_edge("node_pdf_to_md", "node_md_img")
import_builder.add_edge("node_md_img", "node_document_split")
import_builder.add_edge("node_document_split", "node_item_name_recognition")
import_builder.add_edge("node_item_name_recognition", "node_bge_embedding")
import_builder.add_edge("node_bge_embedding", "node_import_milvus")
import_builder.add_edge("node_import_milvus", END)

import_graph = import_builder.compile()
