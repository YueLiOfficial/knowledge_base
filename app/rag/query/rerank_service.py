from langchain.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser

from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.load_prompt import load_prompt
from app.shared.runtime.logger import logger, step_log
from app.infra.llm.providers import llm_prrovider
from app.rag.query.config import *

@step_log("validate_and_get_data")
def validate_and_get_data(state: QueryGraphState) -> tuple[list, list, str]:
    rrf_chunks = state.get("rrf_chunks")
    web_search_docs = state.get("web_search_docs")
    rewritten_query = state.get("rewritten_query")

    if (not rrf_chunks) or (not web_search_docs) or (not rewritten_query):
        logger.error(f"rrf_chunks或web_search_docs或rewritten_query为空")
        raise ValueError(f"rrf_chunks或web_search_docs或rewritten_query为空")

    return rrf_chunks, web_search_docs, rewritten_query

@step_log("merge_answer")
def merge_answer(rrf_chunks: list, web_search_docs: list) -> list[dict]:
    merged_chunks = []

    for chunk in rrf_chunks:
        merged_chunks.append({
            "title": chunk.get("title"),
            "text": chunk.get("content"),
            "url": "",
            "score":0.0,
            "type": "milvus"
        })

    for doc in web_search_docs:
        merged_chunks.append({
            "title": doc.get("title"),
            "text": doc.get("snippet"),
            "url": doc.get("url"),
            "score":0.0,
            "type": "web"
        })

    return merged_chunks

@step_log("create_query_answer_pair")
def create_query_answer_pair(rewritten_query: str, merged_chunks:list[dict]) -> list[list]:
    query_answer_pair = []

    query_len = llm_prrovider.compute_tokens_num(rewritten_query)

    for chunk in merged_chunks:
        answer = chunk["text"]
        chunk_answer_len = llm_prrovider.compute_tokens_num(answer)
        if chunk_answer_len > (RERANK_MAX_INPUT_TOKENS - 4 - query_len):
            llm_model = llm_prrovider.llm_model()
            char_len = max(
                int((RERANK_MAX_INPUT_TOKENS - 4 - query_len) / RERANK_SUMMARY_CHAR_RATIO),
                RERANK_MIN_SUMMARY_CHARS
            )
            prompt = load_prompt("rerank_text_refine", question=rewritten_query, answer=answer, limit=char_len)
            messages = [HumanMessage(content=prompt)]

            chains = llm_model | StrOutputParser()

            logger.debug(f"原始文本: {answer}, 开始精炼")
            answer = chains.invoke(messages)
            logger.debug(f"已精炼为: {answer}")

        query_answer_pair.append([rewritten_query, answer])

    return query_answer_pair

@step_log("call_reranker_compute_score_and_rank")
def call_reranker_compute_score_and_rank(query_answer_pair: list[list], merged_chunks: list[dict]):
    score_list = llm_prrovider.compute_score(query_answer_pair)

    for score, chunk in zip(score_list, merged_chunks): # type: ignore
        chunk["score"] = score

    merged_chunks.sort(key=lambda x: x["score"], reverse=True)

@step_log("dynamic_topk")
def dynamic_topk(merged_chunks: list[dict]) -> list[dict]:
    max_len = min(RERANK_MAX_TOPK, len(merged_chunks))
    min_len = RERANK_MIN_TOPK
    
    top_k = max_len

    if min_len < max_len:
        for index in range(min_len - 1, max_len - 1):
            current_score = merged_chunks[index]["score"]
            next_score = merged_chunks[index + 1]["score"]

            abs_score = current_score - next_score
            ratio = abs_score / current_score

            if (abs_score > RERANK_GAP_ABS) or (ratio > RERANK_GAP_RATIO):
                top_k = index + 1
                break

    merged_chunks = merged_chunks[: top_k]
    logger.info(f"动态截取完成，共截取出{len(merged_chunks)}条数据")
    return merged_chunks

@step_log("rerank_documents")
def rerank_documents(state: QueryGraphState) -> QueryGraphState:
    """
    重排序服务：
    1. 合并 RRF 和 Web Search 的文档
    2. 使用 BGE Reranker 模型计算相关性得分
    3. 根据得分动态截断，智能截取 TopK
    4. 回写 reranked_docs
    """

    rrf_chunks, web_search_docs, rewritten_query = validate_and_get_data(state)

    merged_chunks = merge_answer(rrf_chunks, web_search_docs)

    query_answer_pair = create_query_answer_pair(rewritten_query, merged_chunks)

    call_reranker_compute_score_and_rank(query_answer_pair, merged_chunks)

    reranked_docs = dynamic_topk(merged_chunks)

    state["reranked_docs"] = reranked_docs

    return state