from datetime import datetime
from pathlib import Path
import uuid

from fastapi import BackgroundTasks, FastAPI, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from app.api.server.web_assets import register_web_routes
import uvicorn
from app.api.schema.import_schema import UploadResponseSchema, StatusResponseSchema
from app.process.import_.agent.state import create_default_state
from app.process.import_.agent.main_graph import import_graph
from app.shared.utils.task_utils import get_done_task_list, get_running_task_list, get_task_status, update_task_status

app = FastAPI()

# 统一页面：GET /html 与 /static（资源带版本号且禁缓存）
register_web_routes(app)

# 跨源配置：统一页面可能由 query_server(8001) 提供，此时上传与状态轮询属于跨源请求，
# 缺少该中间件时浏览器会丢弃响应（服务端返回 200 但前端报 CORS 错误）。
# 与 query_server 的配置保持一致。
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
def check_health() -> dict[str, bool]:
    """健康检查接口，供前端探测导入服务是否可用。

    返回结构与 query_server 的同名接口保持一致，便于前端统一处理。

    Returns:
        dict[str, bool]: 固定返回 {"ok": True}，表示服务进程可用。
    """
    return {"ok": True}

def run_import_graph_invoke(task_id: str, local_dir:str, local_file_path: str):
    state = create_default_state(
        task_id=task_id,
        local_dir=local_dir,
        local_file_path=local_file_path
    ) # type: ignore
    try:
        update_task_status(task_id, status_name="processing")
        import_graph.invoke(state)
        update_task_status(task_id, status_name="completed")
    except Exception:
        update_task_status(task_id, status_name="failed")

# 上传文件接口
@app.post("/upload")
async def upload(task:BackgroundTasks, files: list[UploadFile]):
    file = files[0]

    task_id = str(uuid.uuid4())

    local_dir_path = Path(__file__).parents[3] / "output" / datetime.now().strftime('%Y%m%d_%H%M%S') / task_id
    if not local_dir_path.is_dir():
        local_dir_path.mkdir(parents=True, exist_ok=True)
    
    local_file_path = local_dir_path / file.filename # type: ignore
    file_data = await file.read()
    local_file_path.write_bytes(file_data)

    task.add_task(
        run_import_graph_invoke,
        task_id=task_id,
        local_dir=str(local_dir_path),
        local_file_path=str(local_file_path)
    )

    return UploadResponseSchema(
        msg=f"文件{file.filename}上传成功, 请等待解析",
        task_ids=[task_id]
    )

# 查询状态
@app.get("/status/{task_id}")
def status(task_id: str):
    status = get_task_status(task_id)
    running_list = get_running_task_list(task_id)
    done_list = get_done_task_list(task_id)

    return StatusResponseSchema(
        task_id=task_id,
        status=status,
        running_list=running_list,
        done_list=done_list
    )

if __name__ == "__main__":
    uvicorn.run("app.api.server.import_server:app", host="127.0.0.1", port=8000, reload=True)