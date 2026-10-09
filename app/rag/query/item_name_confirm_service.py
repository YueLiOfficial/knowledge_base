import json
from typing import Any
from langchain.messages import HumanMessage
from langchain_core.output_parsers import JsonOutputParser

from app.infra.vectorstore.milvus_gateway import milvus_gateway
from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.load_prompt import load_prompt
from app.shared.runtime.logger import logger, step_log
from app.shared.clients.mongo_history_utils import save_chat_message
from app.shared.clients.mongo_history_utils import get_recent_messages
from app.infra.llm.providers import llm_prrovider
from app.rag.query.config import *

@step_log("dedupe_keep_order")
def dedupe_keep_order(names: list) -> list:
    """列表去重并保持原有顺序。

    同一标准型号可能被多个模糊主体召回，因此候选列表需要去重。

    Args:
        names: 待去重的名称列表，元素可为任意类型。

    Returns:
        list: 去重后的列表，保留首次出现的顺序；空值与非字符串元素会被转成字符串后去重。
    """
    seen: set[str] = set()
    result: list = []
    for name in (names or []):
        if name is None:
            continue
        key = str(name).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result

@step_log("validate_and_get_data")
def validate_and_get_data(state: QueryGraphState) -> tuple[str, str]:
    session_id = state.get("session_id")
    original_query = state.get("original_query")

    if (not session_id) or (not original_query):
        logger.error(f"session_id 或 original_query为空，请传入正确参数")
        raise ValueError(f"session_id 或 original_query为空，请传入正确参数")

    return session_id, original_query

@step_log("get_history")
def get_history(session_id: str) -> list[dict]:
    """ 获取有效的历史消息 """

    history_list = get_recent_messages(session_id)

    logger.info(f"提取到{len(history_list)}条历史消息")
    # 提取有效的消息
    return_history_list = [history for history in history_list if history.get("item_names")]
    logger.info(f"过滤掉无效消息后，剩余{len(return_history_list)}条历史消息")

    return return_history_list

@step_log("get_item_name_and_rewritten")
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
                    f"模型改写后的问题: {history.get('rewritten_query')}, \
                        模型回答: {history.get('text')[:50]}, 提取出的item_name: {history.get('item_names')}" # type: ignore
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

@step_log("select_item_name")
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
            continue

        this_list = []

        # 最终结果: {llm_item_name: [{"item_name": xx, "score": xx}, {}]}
        for res in rel_res:
            this_list.append({
                "item_name": res.get("entity", {}).get("item_name"),
                "score": res.get("distance")
            })

        milvus_res[item_name] = this_list
    
    return milvus_res

@step_log("get_confirmed_item_name")
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
            option_list.extend(middle_score_list)
            logger.info(f"{llm_item_name}取到了可选择的item_name: {middle_score_list}")

    return {
        "confirmed_item_name": confirmed_list,
        "option_item_name": option_list
    }

@step_log("change_state_property")
def change_state_property(state: QueryGraphState, confirmed_and_option_item_name: dict, rewritten_query: str):
    """ 更新state

    候选分支除了写入人类可读的 answer 文案外，同时把候选列表写入
    state["option_item_names"]，供前端渲染可交互的选项卡片；
    answer 的文案格式保持不变，兼容仍按文本解析的旧前端。
    """
    
    confirmed_item_name = confirmed_and_option_item_name.get("confirmed_item_name")
    option_item_name = confirmed_and_option_item_name.get("option_item_name")

    # 去重保序：同一标准型号可能被多个模糊主体召回，避免选项重复展示
    confirmed_item_name = dedupe_keep_order(confirmed_item_name or [])
    option_item_name = dedupe_keep_order(option_item_name or [])

    # 默认清空候选，避免上一次流程残留
    state["option_item_names"] = []

    if confirmed_item_name:
        state["item_names"] = confirmed_item_name
        state["rewritten_query"] = rewritten_query
    elif option_item_name:
        state["option_item_names"] = option_item_name
        state["answer"] = f"请在以下选项中选择要咨询的产品: {option_item_name}"
    else:
        state["answer"] = "无法识别您要咨询的产品，请提供产品准确的名称"

@step_log("save_history")
def save_history(state: QueryGraphState):
    """ 保存历史消息

    注意：本函数在候选列表确定「之前」由 confirm_item_name 调用，
    因此 state["option_item_names"] 此时通常为空。save_chat_message 对
    option_item_names 采用「None 时不写入」的语义，避免把历史中已存的候选
    覆盖为空列表（否则刷新页面后选项卡片会丢失）。
    """
    
    option_item_names = state.get("option_item_names") or None

    save_chat_message(
        session_id=state.get("session_id"),
        role="user",
        text=state.get("original_query"),
        rewritten_query=state.get("rewritten_query"),
        item_names=state.get("item_names"),
        image_urls=[],
        option_item_names=option_item_names
    )

@step_log("apply_forced_item_name")
def apply_forced_item_name(state: QueryGraphState) -> QueryGraphState | None:
    """直选语义：用户已在候选卡片上明确选定商品，直接锁定，跳过主体确认检索。

    命中直选时：
    1. state["item_names"] 直接设为用户选定的名称，并清空候选列表与前置 answer，
       使 router 判定为「已确认主体」而继续走多路召回流程；
    2. rewritten_query 使用用户选定的标准型号（原问题常为模糊表述，如「华为平板怎么用」，
       直接用选定型号更利于后续检索）；
    3. 历史记录中的 item_names 同步为选定型号，保证后续轮次的历史指代一致。

    Args:
        state: 查询图状态。

    Returns:
        QueryGraphState | None: 命中直选时返回更新后的 state；未直选时返回 None，
        由调用方继续走原有的 LLM 抽取 + 向量检索流程。
    """
    forced = state.get("force_item_names") or []
    forced = dedupe_keep_order(forced)
    if not forced:
        return None

    state["item_names"] = forced
    # 必须清空：残留的候选/answer 会让 router 直接跳到答案输出节点
    state["option_item_names"] = []
    state["answer"] = ""
    # 模糊问题（如"华为平板怎么用"）不利于检索，改写为选定型号本身
    state["rewritten_query"] = "、".join(str(name) for name in forced)

    logger.info(f"命中直选，跳过主体确认检索，直接锁定 item_names: {forced}")

    save_history(state)

    return state

@step_log("confirm_item_name")
def confirm_item_name(state: QueryGraphState) -> QueryGraphState:
    """
    意图确认服务：
    1. 若用户已直选商品（state["force_item_names"] 非空），直接锁定并跳过后续检索
    2. 结合历史对话提取商品名
    3. 将模糊问题改写为完整独立的精准问题
    4. 在 Milvus 向量库中进行混合搜索
    5. 根据评分高低自动对齐标准型号，或生成反问让用户手动确认
    6. 同步历史记录到 MongoDB
    """

    # 直选优先：命中则完全跳过 LLM 抽取与向量检索，避免二次检索
    forced_state = apply_forced_item_name(state)
    if forced_state is not None:
        return forced_state

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