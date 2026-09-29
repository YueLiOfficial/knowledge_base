from app.process.import_.agent.state import ImportGraphState
from app.shared.runtime.logger import logger
from pathlib import Path

def resolve_input_file(state: ImportGraphState) -> ImportGraphState:
    """
    入口识别服务：
    1. 校验 local_file_path
    2. 识别文件类型（PDF / Markdown）
    3. 回写 is_pdf_read_enabled / is_md_read_enabled
    4. 回写 pdf_path / md_path / file_title
    """

    # 校验路径为非空
    local_file_path = state.get("local_file_path")
    if not local_file_path:
        logger.error(f"local_file_path为空, 请输入文件路径!")
        raise ValueError(f"local_file_path为空, 请输入文件路径!")

    # 检查文件类型
    if local_file_path.lower().endswith(".md"):
        state["md_path"] = local_file_path
        state["is_md_read_enabled"] = True
        state["pdf_path"] = None
        state["is_pdf_read_enabled"] = False
    elif local_file_path.lower().endswith(".pdf"):
        state["md_path"] = None
        state["is_md_read_enabled"] = False
        state["pdf_path"] = local_file_path
        state["is_pdf_read_enabled"] = True    
    else:
        logger.error(f"文件{local_file_path}, 既不是pdf也不是md, 当前项目无法处理!")
        raise ValueError(f"文件{local_file_path}, 既不是pdf也不是md, 当前项目无法处理!")

    # 获取文件名
    local_file_path_obj: Path = Path(local_file_path)

    if not local_file_path_obj.is_file():
        logger.error(f"{local_file_path}不存在或为目录")
        raise ValueError(f"{local_file_path}不存在或为目录")

    state["file_title"] = local_file_path_obj.stem

    return state
