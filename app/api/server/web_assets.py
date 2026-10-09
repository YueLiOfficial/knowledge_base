"""统一页面（web/）的资源装配模块。

两个服务都需要提供同一套前端（web/index.html + style.css + app.js），
本模块把这段装配逻辑收敛到一处，避免在两个 server 里各写一遍。

包含两项能力：
1. 静态资源挂载，并强制浏览器每次校验（避免复用旧 JS/CSS）；
2. 返回 index.html 时注入资源版本号，使前端改动后刷新即生效。

注意：前端是纯静态资源、文件名不含构建哈希，若不显式禁止缓存，
就会出现「后端已更新、页面仍在跑旧 app.js」的现象。此处统一处理。
"""
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# 项目根目录下的统一前端目录
WEB_DIR = Path(__file__).resolve().parents[3] / "web"

# HTML 中的原始资源引用 → 带版本号的形式
_ASSET_REWRITES = (
    ('href="/static/style.css"', 'href="/static/style.css?v={v}"'),
    ('src="/static/app.js"', 'src="/static/app.js?v={v}"'),
)

# 禁缓存响应头：前端资源无构建哈希，必须每次校验
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class NoCacheStaticFiles(StaticFiles):
    """在默认静态文件响应上追加禁缓存头。"""

    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        for key, value in _NO_CACHE_HEADERS.items():
            response.headers[key] = value
        return response


def _asset_version() -> str:
    """资源版本号：取 index.html 的最后修改时间（秒）。

    每次请求都读磁盘，保证改完前端刷新即生效；取不到时回退 "0"，
    仅失去缓存失效能力，不影响功能。

    Returns:
        str: 版本号字符串。
    """
    try:
        return str(int((WEB_DIR / "index.html").stat().st_mtime))
    except OSError:
        return "0"


def render_index_html() -> str:
    """读取 index.html 并为静态资源链接注入版本号。

    Returns:
        str: 可直接返回给浏览器的 HTML 文本；文件缺失时返回提示页。
    """
    index_path = WEB_DIR / "index.html"
    try:
        html = index_path.read_text(encoding="utf-8")
    except OSError as e:
        return f"<h1>前端页面缺失</h1><p>{index_path} 读取失败：{e}</p>"

    version = _asset_version()
    for source, target in _ASSET_REWRITES:
        html = html.replace(source, target.format(v=version))
    return html


def register_web_routes(app: Any) -> None:
    """把统一页面的路由注册到给定应用上。

    注意：这里刻意不使用 include_router——FastAPI 在复制路由时会访问 route.path，
    而 Mount（/static 静态目录）没有该属性，会被静默跳过，导致静态资源 404。
    因此改为直接把路由对象追加进 app.router.routes。

    Args:
        app: FastAPI 应用实例。

    Returns:
        None
    """
    for route in build_web_router().routes:
        app.router.routes.append(route)


def build_web_router() -> APIRouter:
    """构建统一页面的路由：GET /html 与 /static 静态资源。

    Returns:
        APIRouter: 含页面路由与静态资源挂载的路由对象。
    """
    router = APIRouter()

    @router.get("/html")
    def html() -> HTMLResponse:
        """返回统一页面（资源链接带版本号，且不做强缓存）。"""
        return HTMLResponse(content=render_index_html(), headers=_NO_CACHE_HEADERS)

    router.mount("/static", NoCacheStaticFiles(directory=str(WEB_DIR)), name="static")

    return router
