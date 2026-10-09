from app.process.query.agent.state import QueryGraphState
from app.rag.query.item_name_confirm_service import get_history
from app.shared.clients.mongo_history_utils import save_chat_message
from app.shared.runtime.load_prompt import load_prompt
from app.shared.utils.task_utils import push_to_session
from app.shared.utils.sse_utils import SSEEvent
from app.shared.runtime.logger import logger, step_log
from app.infra.llm.providers import llm_prrovider
import re

@step_log("answer_exits_in_state")
def answer_exits_in_state(state: QueryGraphState) -> bool:
    answer = state.get("answer")

    if answer:
        return True
    else:
        return False

@step_log("validate_and_get_data")
def validate_and_get_data(state: QueryGraphState) -> tuple[list, str, str, list, bool]:
    reranked_docs = state.get("reranked_docs")
    rewritten_query = state.get("rewritten_query")
    session_id = state.get("session_id")
    item_names = state.get("item_names")
    is_stream = state.get("is_stream", True)

    if (not reranked_docs) or (not rewritten_query) or (not session_id) or (not item_names):
        logger.error(f"reanked_docs 或 rewritten_query 或 session_id 或 item_names为空")
        raise ValueError(f"reanked_docs 或 rewritten_query 或 session_id 或 item_names为空")

    return reranked_docs, rewritten_query, session_id, item_names, is_stream

@step_log("get_prompt")
def get_prompt(context_docs: list, history_list: list, item_names_list: list, query:str) -> str:
    context = ""
    for doc in context_docs:
        context += f"标题:{doc['title']}, 来源{'向量数据库' if doc['type'] == 'milvus' else '网络搜索'}, \
            置信度:{doc['score']}, 内容{doc['text']} \n"

    # 拼接历史记录
    history_text: str = ""
    history_new = []
    if history_list:
        for history in history_list:
            if history.get("role") == "user":
                history_new.append(
                    f"用户提问: {history.get('text')}, 改写后的提问: {history.get('rewritten_query')}, \
                        提取出的item_name: {history.get('item_names')}")
            else:
                history_new.append(
                    f"模型改写后的问题: {history.get('rewritten_query')}, \
                        模型回答: {history.get('text')[:50]}, 提取出的item_name: {history.get('item_names')}" # type: ignore
                )

        history_text = "\n".join(history_new)
    else:
        history_text = "没有有效的历史记录"

    item_names = ", ".join(item_names_list)

    # 拼接提示词
    prompt = load_prompt("answer_out", context=context, history=history_text, item_names=item_names, question=query)

    logger.info(f"提示词拼接完成, 等待大模型回答问题")

    return prompt

@step_log("call_llm_get_answer")
def call_llm_get_answer(prompt: str, is_stream: bool, session_id: str) -> str:
    llm_model = llm_prrovider.llm_model()

    answer = ''
    if is_stream:
        llm_output_chunks = llm_model.stream(prompt)

        for chunk in llm_output_chunks:
            token = chunk.content

            push_to_session(
                session_id=session_id,
                event=SSEEvent.DELTA,
                data={"delta": token}
            )

            answer += token # type: ignore
    else:
        llm_output = llm_model.invoke(prompt)
        answer = llm_output.content

    logger.info(f"大模型的回答: {answer}")

    return answer # type: ignore

@step_log("get_image_urls")
def get_image_urls(context_docs: list[dict]) -> list:
    image_urls = []

    reg = re.compile(r"\!\[.*?\]\((.*?)\)")

    for doc in context_docs:
        url:str = doc["url"]
        if url:
            if url.endswith((".png", ".jpg", ".gif", ".jpeg", ".svg")):
                image_urls.append(url)

        text = doc["text"]
        if text:
            images = reg.findall(text)
            if images:
                image_urls.extend(images)

    logger.info(f"完成图片获取")

    return image_urls

@step_log("save_history")
def save_history(state: QueryGraphState):
    # option_item_names 仅在候选分支有值；为 None 时 save_chat_message 不写入该字段，
    # 从而不会覆盖历史中已有的候选数据。
    save_chat_message(
        session_id=state["session_id"],
        role="assistant",
        text=state["answer"],
        rewritten_query=state["rewritten_query"],
        item_names=state["item_names"],
        image_urls=state["image_urls"],
        option_item_names=state.get("option_item_names") or None,
    )

@step_log("generate_answer")
def generate_answer(state: QueryGraphState) -> QueryGraphState:
    """
    答案生成服务：
    1. 检查前置答案（如有追问或拒绝回答，直接输出）
    2. 构建 Prompt（用户问题 + 历史对话 + TopK 文档）
    3. 调用 LLM 生成最终答案（支持流式推送）
    4. 从引用文档中提取图片 URL
    5. 写入 MongoDB 历史记录
    6. 回写 answer 和 image_urls
    """
    ""

    has_answer = answer_exits_in_state(state)

    if not has_answer:
        reranked_docs, rewritten_query, session_id, item_names, is_stream = validate_and_get_data(state)

        history_list = get_history(session_id)

        prompt = get_prompt(reranked_docs, history_list, item_names, rewritten_query)

        answer = call_llm_get_answer(prompt, is_stream, session_id)

        image_urls = get_image_urls(reranked_docs)

        state["answer"] = answer
        state["image_urls"] = image_urls

    save_history(state)

    return state