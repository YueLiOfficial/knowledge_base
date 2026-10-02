import json
from pathlib import Path
from typing import Any

from pymilvus import DataType

from app.process.import_.agent.state import ImportGraphState
from app.shared.runtime.logger import logger, step_log
from app.infra.vectorstore.milvus_gateway import milvus_gateway

@step_log("validata_and_get_data")
def validata_and_get_data(state: ImportGraphState) -> list[dict[str, Any]]:
    chunks = state.get("embeddings_content")

    if not chunks:
        logger.error(f"embeddings_content为空")
        raise ValueError(f"embeddings_content为空")

    return chunks

@step_log("create_collection")
def create_collection() -> None:
    client = milvus_gateway.client

    if not client:
        logger.error(f"MilvusClient创建失败")
        raise RuntimeError(f"MilvusClient创建失败")

    if client.has_collection(milvus_gateway.chunks_collection):
        logger.info(f"已存在集合{milvus_gateway.chunks_collection}, 无需重复创建")
        return 

    schema = client.create_schema(
        auto_id=True,
        enable_dynamic_field=True
    )
    schema.add_field(field_name="chunk_id", datatype=DataType.INT64, is_primary=True)
    schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=65535)
    schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="parent_title", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="part", datatype=DataType.INT8)
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

    client.create_collection(
        collection_name=milvus_gateway.chunks_collection,
        schema=schema,
        index_params=index_params
    )

    logger.info(f"集合{milvus_gateway.chunks_collection}创建成功")

@step_log("insert_chunks_to_milvus")
def insert_chunks_to_milvus(chunks: list[dict[str, Any]]) -> None:
    client = milvus_gateway.client

    file_title = chunks[0].get("file_title")

    if not client:
        logger.error(f"MilvusClient创建失败")
        raise RuntimeError(f"MilvusClient创建失败")

    client.delete(
        collection_name=milvus_gateway.chunks_collection,
        filter=f"file_title == '{file_title}'"
    )

    res = client.insert(
        collection_name=milvus_gateway.chunks_collection,
        data=chunks
    )

    logger.info(f"向集合{milvus_gateway.chunks_collection}添加数据完成, 共添加{res['insert_count']}条数据")

@step_log("index_chunks")
def index_chunks(state: ImportGraphState) -> ImportGraphState:
    """
    入库服务：
    1. 准备集合 schema 和索引
    2. 根据 item_name 删除旧数据
    3. 批量插入新的 chunks
    4. 回写 chunk_id 等入库结果
    """
    chunks = validata_and_get_data(state)

    create_collection()

    insert_chunks_to_milvus(chunks)

    return state