from datetime import datetime
import json
from mimetypes import guess_type
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.schema.query_schema import ClearHistorySchema, NotStreamResponseSchema, SearchHistoryItemSchema, SearchHistorySchema, StreamRequestSchema, StreamResponseSchema
from app.process.query.agent.state import create_query_default_state
from app.shared.clients.mongo_history_utils import clear_history, get_recent_messages
from app.shared.runtime.logger import logger
from app.shared.utils.sse_utils import SSEEvent, create_sse_queue, push_to_session, sse_generator
from app.process.query.agent.main_graph import query_graph
from app.shared.utils.task_utils import clear_task, get_done_task_list, update_task_status

app = FastAPI()

# 统一页面静态资源目录（结构 web/index.html，资源 /static/style.css、/static/app.js）
WEB_DIR = Path(__file__).parents[3] / "web"

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
def check_health():
    # logger.info(f"{datetime.now()}完成了本次健康状态检查")
    return {"ok": True}

@app.get("/html")
def html():
    # 统一页面：结构在 web/index.html，样式与脚本由同目录的 style.css / app.js 提供
    html_path = Path(__file__).parents[3] / "web" / "index.html"
    
    return FileResponse(
        path = str(html_path),
        media_type=guess_type(html_path.name)[0]
    )

@app.get("/stream/{session_id}")
def get_stream_msg(session_id: str, request: Request):

    return  StreamingResponse(
        sse_generator(session_id,request),
        media_type="text/event-stream"
    )

def invoke_query(session_id: str, query: str, is_stream: bool, force_item_names: list[str] | None = None):
    try:
        clear_task(session_id)

        if is_stream:
            create_sse_queue(session_id)

        state = create_query_default_state(
                session_id = session_id,
                original_query = query,
                is_stream = is_stream,
                force_item_names = force_item_names or []
            )

        logger.info(f"{session_id}开始进行解析")
        update_task_status(session_id, "processing", is_stream)
        state = query_graph.invoke(state)
        logger.info(f"{session_id}解析完成")
        update_task_status(session_id, "completed", is_stream)
    
        if is_stream:
            push_to_session(
                session_id,
                SSEEvent.FINAL,
                {
                    "answer": state.get("answer"),
                    "status": "completed",
                    "image_urls": state.get("image_urls"),
                    "option_item_names": state.get("option_item_names", [])
                }
            )

        return state

    except Exception as e:
        update_task_status(session_id, "failed", is_stream)
        logger.exception(f"{session_id}查询流程报错")
        push_to_session(session_id, SSEEvent.ERROR, {"error": {str(e)}})

@app.post("/query")
def query(param: StreamRequestSchema, task: BackgroundTasks):
    query = param.query
    session_id = param.session_id
    is_stream = param.is_stream
    force_item_names = param.force_item_names

    if is_stream:
        task.add_task(
            invoke_query,
            session_id=session_id,
            query=query,
            is_stream=is_stream,
            force_item_names=force_item_names
        )

        return StreamResponseSchema(
            session_id=session_id,
            message=f"{session_id}正在进行解析..."
        )
    else:
        state = invoke_query(
            session_id=session_id,
            query=query,
            is_stream=is_stream,
            force_item_names=force_item_names
        )

        done_list = get_done_task_list(session_id)

        assert state is not None
        return NotStreamResponseSchema(
            session_id=session_id,
            message=f"{session_id}结果解析完成",
            answer=state.get("answer", ""),
            done_list=done_list,
            image_urls=state.get("image_urls", []),
            option_item_names=state.get("option_item_names", [])
        )

# 删除历史记录接口
@app.delete("/history/{session_id}")
def delete_history(session_id: str):
    delete_count = clear_history(session_id=session_id)

    logger.info(f"{session_id}删除了{delete_count}条数据")

    return ClearHistorySchema(
        message=f"{session_id}删除了{delete_count}条数据",
        delete_count=delete_count
    )

# 获取历史记录接口
@app.get("/history/{session_id}")
def get_history(session_id: str, limit: int=10):
    items = get_recent_messages(session_id=session_id, limit=limit)

    return SearchHistorySchema(
        session_id=session_id,
        items=[
            SearchHistoryItemSchema(
                id=str(item.get("id")),
                session_id=item.get("session_id", ''),
                role=item.get("role", ''),
                text=item.get("text", ''),
                rewritten_query=item.get("rewritten_query", ''),
                item_names=item.get("item_names", []),
                image_urls=item.get("image_urls", []),
                ts=item.get("ts", ''),
                option_item_names=item.get("option_item_names") or []
            ) for item in items
        ]
    )

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.api.server.query_server:app", port=8001, reload=True)
