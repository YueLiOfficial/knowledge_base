from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.logger import logger, step_log
from app.rag.query.config import *

@step_log("validate_and_get_data")
def validate_and_get_data(state: QueryGraphState):
    embedding_chunks = state.get("embedding_chunks")
    hyde_embedding_chunks = state.get("hyde_embedding_chunks")

    if (not embedding_chunks) or (not hyde_embedding_chunks):
        logger.error(f"embedding_chunks 或 hyde_embedding_chunks 为空，请传入正确参数")
        raise ValueError(f"embedding_chunks 或 hyde_embedding_chunks 为空，请传入正确参数")

    return embedding_chunks, hyde_embedding_chunks

@step_log("validate_and_get_data")
def rrf_reranker(datalist: list[tuple[float, list]], k: int = 60):
    score_dict = {}
    chunk_dict = {}
    
    for weight, chunks in datalist:
        for rank, chunk in enumerate(chunks, start=1):
            chunk_id = chunk.get("chunk_id")
            score_dict[chunk_id] = score_dict.get(chunk_id, 0) + weight * (1 / (rank + k))
            chunk_dict[chunk_id] = chunk

    rrf_list = []
    for chunk_id, chunk in chunk_dict.items():
        chunk["score"] = score_dict.get(chunk_id, 0)
        rrf_list.append(chunk)

    rrf_list.sort(key=lambda x: x["score"], reverse=True)
    logger.info(f"进行rrf_reranker后共获得了{len(rrf_list)}条数据")

    # 截取topk
    rrf_list = rrf_list[: MILVUS_CHUNK_TOP_K]

    return rrf_list

@step_log("validate_and_get_data")
def fuse_by_rrf(state: QueryGraphState) -> QueryGraphState:
    """
    RRF 融合服务：
    1. 合并来自不同检索源的文档列表
    2. 应用 RRF 算法消除分数差异
    3. 给出综合排名最高的文档列表（Top 10）
    4. 回写 rrf_chunks
    """

    embedding_chunks, hyde_embedding_chunks = validate_and_get_data(state)

    data_list = [
        (0.5, embedding_chunks),
        (0.5, hyde_embedding_chunks)
    ]

    rrf_chunks = rrf_reranker(data_list)

    state["rrf_chunks"] = rrf_chunks

    return state