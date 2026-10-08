import asyncio
import json

from agents.mcp import MCPServerStreamableHttp

from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.logger import logger, step_log
from app.infra.config.providers import infra_config

@step_log("validate_and_get_data")
def validate_and_get_data(state: QueryGraphState) -> str:
    rewritten_query = state.get("rewritten_query")

    if not rewritten_query:
        logger.error(f"rewritten_query 为空，请传入正确的数据")
        raise ValueError(f"rewritten_query 为空，请传入正确的数据")

    return rewritten_query

@step_log("get_web_search_answer")
async def get_web_search_answer(rewritten_query: str):
    # 创建mcp-server
    mcp_server = MCPServerStreamableHttp(
        name="bailian-web-search-server",
        params={ # type: ignore
            "url": infra_config.mcp_config.mcp_base_url,
            "headers": {"Authorization": f"Bearer {infra_config.lm_config.api_key}"},
            "timeout": 300,
        },
        cache_tools_list=True,
        max_retry_attempts=3
    )

    # 连接服务器
    await mcp_server.connect()

    try:
        tool_list = await mcp_server.list_tools()
        logger.debug(f"{mcp_server.name}有以下工具: {tool_list}")

        res = await mcp_server.call_tool(
            tool_name="bailian_web_search",
            arguments={
                "query": rewritten_query,
                "count": 10
            }
        )

        return res
    except Exception as e:
        logger.exception(f"MCP联网查询失败: {e}")
    finally:
        await mcp_server.cleanup()

@step_log("search_by_web")
def search_by_web(state: QueryGraphState) -> QueryGraphState:
    """
    网络搜索服务：
    1. 通过 MCP 协议异步调用百炼联网搜索接口
    2. 将用户的查询转化为实时的、结构化的网络搜索结果
    3. 包含标题、链接和摘要
    4. 回写 web_search_docs
    """

    rewritten_query = validate_and_get_data(state)

    web_search_answer = asyncio.run(get_web_search_answer(rewritten_query))

    web_search_str = web_search_answer.content[0].text # type: ignore

    web_search_docs = json.loads(web_search_str)

    return web_search_docs