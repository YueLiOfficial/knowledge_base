from app.infra.vectorstore.milvus_gateway import milvus_gateway
from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.logger import logger, step_log
from app.infra.llm.providers import llm_prrovider
from app.rag.query.config import *

@step_log("validate_and_get_data")
def validate_and_get_data(state: QueryGraphState) -> tuple[str, list]:
    rewritten_query = state.get("rewritten_query")
    item_names = state.get("item_names")

    if (not rewritten_query) or (not item_names):
        logger.error(f"rewritten_query 或 item_name 为空，请传入正确参数")
        raise ValueError(f"rewritten_query 或 item_name 为空，请传入正确参数")

    return rewritten_query, item_names

@step_log("get_embedding_search_answer")
def get_embedding_search_answer(rewritten_query: str, item_names: list) -> list[dict]:
    embedding_dict = llm_prrovider.generate_embeddings([rewritten_query])

    dense = embedding_dict["dense"][0]
    sparse = embedding_dict["sparse"][0]

    reqs = milvus_gateway.create_requests(
        dense_vector=dense,
        sparse_vector=sparse,
        expr=f"item_name in {item_names}",
        limit=MILVUS_CHUNK_TOP_K * 2
    )

    result = milvus_gateway.hybrid_search(
        collection_name=milvus_gateway.chunks_collection,
        reqs=reqs,
        ranker_weights=(0.5, 0.5),
        norm_score=True,
        limit=MILVUS_CHUNK_TOP_K,
        output_fields=["chunk_id", "file_title", "parent_title", "title", "part", "content", "item_name"]
    )

    return result[0] # type: ignore

@step_log("parse_result")
def parse_result(embedding_result) -> list[dict]:
    result_list = []

    if embedding_result:
        for res in embedding_result:
            entity = res.get("entity", {})

            result_list.append({
                "chunk_id": entity.get("chunk_id"),
                "file_title": entity.get("file_title"),
                "parent_title": entity.get("parent_title"),
                "title": entity.get("title"),
                "part": entity.get("part"),
                "content": entity.get("content"),
                "item_name": entity.get("item_name"),
                "score": res.get("distance", 0),
                "type": "milvus"
            })

    logger.info(f"完成了向量检索，共检索出{len(result_list)}条结果")

    return result_list

@step_log("search_by_embedding")
def search_by_embedding(state: QueryGraphState) -> list:
    """
    向量检索服务：
    1. 根据改写后的问题和限定的商品范围
    2. 利用 BGEM3 混合检索（稠密+稀疏）技术
    3. 从 Milvus 向量数据库中召回 Top-K 最相关的知识切片
    4. 回写 embedding_chunks
    """

    rewritten_query, item_names = validate_and_get_data(state)

    embedding_list = get_embedding_search_answer(rewritten_query, item_names)

    embedding_chunks = parse_result(embedding_list)

    return embedding_chunks