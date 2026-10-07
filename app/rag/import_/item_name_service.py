from typing import Any
from langchain.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser
from pymilvus import DataType
from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.config import ITEM_NAME_CONTEXT_CHUNK_K, ITEM_NAME_CONTEXT_TOTAL_MAX_CHARS
from app.shared.runtime.logger import logger, step_log
from pathlib import Path
import json
from app.infra.llm.providers import llm_prrovider
from app.shared.runtime.load_prompt import load_prompt
from app.infra.vectorstore.milvus_gateway import milvus_gateway
from app.infra.llm.providers import llm_prrovider

@step_log("validate_and_get_data")
def validate_and_get_data(state: ImportGraphState) -> tuple[str, list[dict[str, Any]]]:
    md_path = state.get("md_path")
    chunks = state.get("chunks")
    file_title = state.get("file_title")

    if (not md_path) or (not Path(md_path).is_file()):
        logger.error(f"md_path为空或不存在，请传入正确参数")
        raise ValueError(f"md_path为空，请传入正确参数")

    if not file_title:
        file_title = Path(md_path).stem
        logger.warning(f"file_title为空，使用默认值{file_title}")
        state["file_title"] = file_title

    if not chunks:
        json_file = Path(md_path).parent / f"{file_title}.json"
        if not json_file.is_file():
            logger.error(f"{json_file.name}备份文件为空或不存在")
            raise FileNotFoundError(f"{json_file.name}备份文件为空或不存在")

        chunks = json.loads(json_file.read_text(encoding="utf-8"))
        state["chunks"] = chunks

    return file_title, chunks 

@step_log("get_item_name")
def get_item_name(chunks: list[dict[str, Any]], file_title: str) -> str:
    """
    获取item_name.

    Args:
        chunks:
        file_title:
    Returns:
        item_name.
    """

    llm_model = llm_prrovider.llm_model()

    context = ""

    for chunk in chunks[: ITEM_NAME_CONTEXT_CHUNK_K]:
        context += f"标题: {chunk.get('parent_title')}, 内容: {chunk.get('content')}。\n"

    context = context[: ITEM_NAME_CONTEXT_TOTAL_MAX_CHARS]

    prompt = load_prompt("item_name_recognition", file_title=file_title, context=context)

    messages = [
        HumanMessage(content=prompt)
    ]

    chains = llm_model | StrOutputParser()

    item_name = chains.invoke(messages)

    if not item_name:
        item_name = file_title
        logger.warning(f"没有获取到item_name, 已赋值为: {item_name}")

    logger.info(f"已获取到item_name, 为: {item_name}")

    return item_name

@step_log("padding_item_name")
def padding_item_name(chunks: list[dict[str, Any]], item_name: str) -> None:
    """
    将item_name填充进chunks.
    """

    for chunk in chunks:
        chunk["item_name"] = item_name

    logger.info(f"给chunks填充item_name完成")

@step_log("create_item_name_collection")
def create_item_name_collection() -> None:
    """
    在milvus中创建集合。
    """

    client = milvus_gateway.client

    if not client:
        logger.error(f"创建milvus客户端失败")
        raise RuntimeError(f"创建milvus客户端失败")

    # 检查是否存在集合    
    if client.has_collection(milvus_gateway.item_name_collection):
        logger.info(f"已存在集合{milvus_gateway.item_name_collection}, 无需重复创建")
        return

    schema = client.create_schema(
        auto_id = True,
        enable_dynamic_field=True
    )
    schema.add_field(field_name="pk", datatype=DataType.INT64, is_primary=True)
    schema.add_field(field_name="file_title", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="item_name", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=1024)
    schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="dense_vector",
        index_type="HNSW",
        index_name="dense_vector_index",
        metric_type="COSINE",
        params={
            "M": 64,
            "efConstruction": 100
        }
    )
    index_params.add_index(
        field_name="sparse_vector",
        index_type="SPARSE_INVERTED_INDEX",
        index_name="sparse_vector_index",
        metric_type="IP",
        params={"inverted_index_algo": "SINDI"}
    )

    # 集合不存在则创建
    client.create_collection(
        collection_name=milvus_gateway.item_name_collection,
        schema=schema,
        index_params=index_params
    )

    logger.info(f"已创建好集合{milvus_gateway.item_name_collection}")

@step_log("add_item_name_to_milvus")
def add_item_name_to_milvus(item_name: str, file_title: str) -> None:
    """
    将item_name以及相关向量保存到milvus。
    """

    client = milvus_gateway.client

    if not client:
        logger.error(f"创建milvus客户端失败")
        raise RuntimeError(f"创建milvus客户端失败")

    # 先找到这个文档对应的数据删掉，避免数据不一致引起干扰
    client.delete(
        collection_name=milvus_gateway.item_name_collection,
        filter=f"file_title == '{file_title}'"
    )

    # 获取嵌入向量
    vector = llm_prrovider.generate_embeddings([item_name])

    dense_vector = vector["dense"][0]
    sparse_vector = vector["sparse"][0]

    client.insert(
        collection_name=milvus_gateway.item_name_collection,
        data={
            "file_title": file_title,
            "item_name": item_name,
            "dense_vector": dense_vector,
            "sparse_vector": sparse_vector
        }
    )

    logger.info(f"已经将item_name添加到集合{milvus_gateway.item_name_collection}中")

@step_log("recognize_and_index_item_name")
def recognize_and_index_item_name(state: ImportGraphState) -> ImportGraphState:
    """
    主体识别服务：
    1. 基于 chunks 构造上下文
    2. 调用 LLM 识别 item_name
    3. 将 item_name 回填到 state 和 chunks
    4. 同步写入主体名称索引
    """

    # 校验和获取数据
    file_title, chunks = validate_and_get_data(state)

    # 大模型识别item_name
    item_name = get_item_name(chunks, file_title)

    # 更新chunks，添加item_name
    padding_item_name(chunks, item_name)

    # 创建milvus集合
    create_item_name_collection()

    # 将item_name添加到集合中
    add_item_name_to_milvus(item_name, file_title)

    state["chunks"] = chunks
    state["item_name"] = item_name

    return state