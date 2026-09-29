from typing import TypedDict
import copy

class ImportGraphState(TypedDict):
    task_id: str

    # 未确认的文件路径
    local_file_path: str

    # 确认的文件
    md_path: str
    pdf_path:str
    file_title: str
    local_dir: str

    # 文件内容
    md_content: str
    chunks: list[dict]
    item_name: str

    # 文件格式
    is_md_read_enabled: bool
    is_pdf_read_enabled: bool

    # 嵌入向量 
    embeddings_content: list[dict]

    # 模板对象
graph_default_state: ImportGraphState = {
    "task_id": "",
    "is_pdf_read_enabled": False,
    "is_md_read_enabled": False,
    "local_dir": "",
    "local_file_path": "",
    "pdf_path": "",
    "md_path": "",
    "file_title": "",
    "md_content": "",
    "chunks": [],
    "item_name": "",
    "embeddings_content": [],
}

def create_deafult_state(**args):
    new_state = copy.deepcopy(graph_default_state)

    new_state.update(args)

    return new_state

def get_default_state():
    return graph_default_state