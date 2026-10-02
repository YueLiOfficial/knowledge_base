import json
from pathlib import Path
from typing import Any
from app.infra.llm.providers import llm_prrovider
from app.process.import_.agent.state import ImportGraphState
from app.shared.runtime.logger import logger, step_log
from app.rag.import_.config import *

@step_log("validata_and_get_data")
def validata_and_get_data(state: ImportGraphState) -> list[dict[str, Any]]:
    chunks = state.get("chunks")

    if not chunks:
        md_path = state.get("md_path")
        if (not md_path) or (not Path(md_path).is_file()):
            logger.error(f"md_path为空或不存在，请传入正确参数")
            raise ValueError(f"md_path为空，请传入正确参数")

        chunks_file_path = Path(md_path).parent / f"{Path(md_path).stem}.json"
        if not chunks_file_path.is_file():
            logger.error(f"{chunks_file_path.name}备份文件为空或不存在")
            raise FileNotFoundError(f"{chunks_file_path.name}备份文件为空或不存在")
        
        chunks = json.loads(chunks_file_path.read_text(encoding="utf-8"))
        for chunk in chunks:
            chunk["item_name"] = state.get("item_name")

        state["chunks"] = chunks

    return chunks

@step_log("get_chunks_embedding_vector")
def get_chunks_embedding_vector(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    获取每个chunk的嵌入向量
    
    Args:
        chunks:...
    
    Returns:
        [{"dense_vector": [], "sparse_vector": []}, {}, {}]
    """

    vector_list = []

    total_batch = (len(chunks) + EMBEDDING_BATCH_SIZE - 1) // EMBEDDING_BATCH_SIZE

    for batch in range(total_batch):
        batch_chunks = chunks[batch * EMBEDDING_BATCH_SIZE: (batch + 1) * EMBEDDING_BATCH_SIZE]
        texts = []
        for chunk in batch_chunks:
            texts.append(chunk.get("item_name", "")+"_"+chunk.get("content", ""))

        batch_vectors = llm_prrovider.generate_embeddings(texts)
        dense_vectors = batch_vectors["dense"]
        sparse_vectors = batch_vectors["sparse"]

        if len(dense_vectors) != len(sparse_vectors):
            logger.error(f"生成的dense/sparse vector数量不一致")
            raise ValueError(f"生成的dense/sparse vector数量不一致")

        for dense, sparse in zip(dense_vectors, sparse_vectors):
            vector_list.append({
                "dense_vector": dense,
                "sparse_vector": sparse
            })

    logger.info(f"稠密向量和稀疏向量获取完成")

    return vector_list

@step_log("padding_vector_to_chunks")
def padding_vector_to_chunks(chunks: list[dict[str, Any]], vector_list: list[dict[str, Any]]) -> None:
    """将嵌入向量更新到chunk中"""
    
    if len(chunks) != len(vector_list):
        logger.error(f"chunks和vector_list数量不一致")
        raise ValueError(f"chunks和vector_list数量不一致")
    
    for chunk, vector in zip(chunks, vector_list):
        chunk["dense_vector"] = vector["dense_vector"]
        chunk["sparse_vector"] = vector["sparse_vector"]

    logger.info(f"已将稠密向量和稀疏向量添加到chunk中")

@step_log("generate_chunk_embeddings")
def generate_chunk_embeddings(state: ImportGraphState) -> ImportGraphState:
    """
    向量化服务：
    1. 读取 chunks
    2. 生成 dense_vector / sparse_vector
    3. 将向量结果补充回 chunks
    """
    chunks = validata_and_get_data(state)

    vector_list = get_chunks_embedding_vector(chunks)

    padding_vector_to_chunks(chunks, vector_list)

    state["embeddings_content"] = chunks

    return state