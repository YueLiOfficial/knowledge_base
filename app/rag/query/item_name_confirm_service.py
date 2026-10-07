import json
from typing import Any
from langchain.messages import HumanMessage
from langchain_core.output_parsers import JsonOutputParser

from app.infra.vectorstore.milvus_gateway import milvus_gateway
from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.load_prompt import load_prompt
from app.shared.runtime.logger import logger
from app.shared.clients.mongo_history_utils import save_chat_message
from app.shared.clients.mongo_history_utils import get_recent_messages
from app.infra.llm.providers import llm_prrovider
from app.rag.query.config import *


def validate_and_get_data(state: QueryGraphState) -> tuple[str, str]:
    session_id = state.get("session_id")
    original_query = state.get("original_query")

    if (not session_id) or (not original_query):
        logger.error(f"session_id 或 original_query为空，请传入正确参数")
        raise ValueError(f"session_id 或 original_query为空，请传入正确参数")

    return session_id, original_query

def get_history(session_id: str) -> list[dict]:
    """ 获取有效的历史消息 """

    history_list = get_recent_messages(session_id)

    logger.info(f"提取到{len(history_list)}条历史消息")
    # 提取有效的消息
    return_history_list = [history for history in history_list if history.get("item_names")]
    logger.info(f"过滤掉无效消息后，剩余{len(return_history_list)}条历史消息")

    return return_history_list
    
def get_item_name_and_rewritten(original_query: str, history_list: list[dict]) -> dict[str, Any]:
    """
    通过大模型获取item_name并且重写问题。

    Args:
        original_query:
        history_list:

    Returns:
        {"item_names": [], "rewritten_query": str}
    """
    
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
                    f"模型改写后的问题: {history.get('rewritten_query')}, 模型回答: {history.get('text')[:50]}, \
                        提取出的item_name: {history.get('item_names')}"
                )

        history_text = "\n".join(history_new)
    else:
        history_text = "没有有效的历史记录"

    # 拼接提示词
    prompt = load_prompt("rewritten_query_and_itemnames", query = original_query, history_text = history_text)

    # 获取大模型
    llm_model = llm_prrovider.llm_model(json_mode=True)

    chains = llm_model | JsonOutputParser()

    messages = [
        HumanMessage(content=prompt)
    ]

    res = chains.invoke(messages)

    return {
        "item_names": res.get("item_names"),
        "rewritten_query": res.get("rewritten_query")
    }

def select_item_name(llm_dict: dict[str, Any]) -> dict[str, list]:
    """
    对大模型给出的item_name进行得分计算。

    Args:
        llm_dict:

    Returns:
        {llm_item_name: [{"item_name": xx, score: xx}, {}, {}]}
    """
    
    item_names = llm_dict["item_names"]

    # 向量化
    item_dict:dict = llm_prrovider.generate_embeddings(item_names)

    milvus_res = {}

    for index, item_name in enumerate(item_names):
        # 获取当前item对应的向量
        dense = item_dict["dense"][index]
        sparse = item_dict["sparse"][index]

        # 混合检索
        reqs = milvus_gateway.create_requests(
            dense_vector=dense,
            sparse_vector=sparse,
            limit=10
        )

        res = milvus_gateway.hybrid_search(
            collection_name=milvus_gateway.item_name_collection,
            reqs=reqs,
            ranker_weights=(0.4, 0.6),
            norm_score=True,
            limit=5,
            output_fields=["item_name"]
        )

        # res = [[{id: xx, distance: xx, entity: {}}, {}, {}]]
        # 取出混合检索的结果
        rel_res = res[0] # type: ignore

        if not rel_res:
            logger.warning(f"{item_name}没有检索到item_name, 流程终止")
            break

        this_list = []

        # 最终结果: {llm_item_name: [{"item_name": xx, "score": xx}, {}]}
        for res in rel_res:
            this_list.append({
                "item_name": res.get("entity", {}).get("item_name"),
                "score": res.get("distance")
            })

        milvus_res[item_name] = this_list
    
    return milvus_res

def get_confirmed_item_name(milvus_res: dict) -> dict:
    """
    根据每个item_name的得分情况，获取可信的结果和可选的结果。

    Args:
        milvus_res:

    Returns:
        {
            "confirmed_item_name": [],
            "option_item_name": []
        }
    """
    
    confirmed_list = [] # 保存能确定的item_name, llm识别出来几个主体就保存几个item_name
    option_list = []    

    for llm_item_name, res in milvus_res.items():
        high_score_list = [item.get("item_name") for item in res if item.get("score", 0) >= ITEM_NAME_CONFIRM_THRESHOLD]
        middle_score_list = [item.get("item_name") for item in res if ITEM_NAME_CANDIDATE_THRESHOLD <= item.get("score", 0) < ITEM_NAME_CONFIRM_THRESHOLD]

        if high_score_list:
            confirmed_list.append(high_score_list[0])
            logger.info(f"{llm_item_name}取到了可信的item_name: {high_score_list[0]}")
            continue

        elif middle_score_list:
            option_list.extend(middle_score_list[:ITEM_NAME_OPTIONS_TOPK])
            logger.info(f"{llm_item_name}取到了可选择的item_name: {middle_score_list[:ITEM_NAME_OPTIONS_TOPK]}")

    return {
        "confirmed_item_name": confirmed_list,
        "option_item_name": option_list
    }

def change_state_property(state: QueryGraphState, confirmed_and_option_item_name: dict, rewritten_query: str):
    """ 更新state """
    
    confirmed_item_name = confirmed_and_option_item_name.get("confirmed_item_name")
    option_item_name = confirmed_and_option_item_name.get("option_item_name")

    if confirmed_item_name:
        state["item_names"] = confirmed_item_name
        state["rewritten_query"] = rewritten_query
    elif option_item_name:
        state["answer"] = f"请在以下选项中选择要咨询的产品: {option_item_name}"
    else:
        state["answer"] = "无法识别您要咨询的产品，请提供产品准确的名称"

def save_history(state: QueryGraphState):
    """ 保存历史消息 """
    
    save_chat_message(
        session_id=state.get("session_id"),
        role="user",
        text=state.get("original_query"),
        rewritten_query=state.get("rewritten_query"),
        item_names=state.get("item_names"),
        image_urls=[]
    )


def confirm_item_name(state: QueryGraphState) -> QueryGraphState:
    """
    意图确认服务：
    1. 结合历史对话提取商品名
    2. 将模糊问题改写为完整独立的精准问题
    3. 在 Milvus 向量库中进行混合搜索
    4. 根据评分高低自动对齐标准型号，或生成反问让用户手动确认
    5. 同步历史记录到 MongoDB
    """

    # 参数校验
    session_id, original_query = validate_and_get_data(state)

    # 获取历史会话消息
    history_list = get_history(session_id)

    # 识别item_name 和 改写问题
    llm_dict = get_item_name_and_rewritten(original_query, history_list)

    confirmed_item_dict = {}
    if llm_dict.get("item_names"):
        milvus_res = select_item_name(llm_dict)
        confirmed_item_dict = get_confirmed_item_name(milvus_res)

    change_state_property(state, confirmed_item_dict, llm_dict["rewritten_query"])

    save_history(state)

    return state