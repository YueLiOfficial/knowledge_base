from app.process.import_.agent.state import ImportGraphState
from app.shared.runtime.logger import logger, step_log
from pathlib import Path
from typing import Any
import re
from app.rag.import_.config import *
from langchain_text_splitters import RecursiveCharacterTextSplitter
import json

@step_log("validate_get_data")
def validate_get_data(state: ImportGraphState) -> tuple[str, str, str]:
    """
    校验数据并替换md_content中的\r\n。
    
    Args:
        state: ImportGraphState
    
    Returns:
        tuple[md_content, md_path, file_title]
    """

    md_content = state.get("md_content")
    md_path = state.get("md_path")
    file_tile = state.get("file_title")

    if (not md_path) or (not Path(md_path).is_file()):
        logger.error(f"md_path为空或不存在, 请传入正确的数据")
        raise ValueError(f"md_path为空或不存在, 请传入正确的数据")

    if not md_content:
        md_content = Path(md_path).read_text(encoding="utf-8")
        state["md_content"] = md_content

    if not file_tile:
        file_tile = Path(md_path).stem
        state["file_title"] = file_tile
        logger.warning(f"file_title为空, 设定为默认值")

    # 将md_content中的\r\n和\r替换成\n
    md_content = md_content.replace("\r\n", "\n").replace("\r", "\n")

    return md_content, md_path, file_tile

@step_log("split_by_title")
def split_by_title(md_content: str, file_title: str) -> list[dict[str, Any]]:
    """
    按照标题对文本进行粗切分。

    Agrs:
        md_content: md文档内容。
        file_title: md文档名字
    
    Returns:
        粗切分后的文本块。
    """

    chunks = []

    current_title = None
    lines_for_title:list = []
    is_in_code:bool = False

    lines = md_content.split("\n")
    for line in lines:
        # 判断空行
        strip_line = line.strip()
        if not strip_line:
            continue

        # 判断代码块
        if strip_line.startswith("```") or strip_line.startswith("~~~"):
            is_in_code = not is_in_code
            lines_for_title.append(strip_line)
            continue

        title_reg = re.compile(r"^\s*#{1,6}\s.+")
        
        if (not is_in_code) and title_reg.match(strip_line):
            # 正常结束本次标题
            if current_title and len(lines_for_title) > 1:
                chunks.append({
                    "file_title": file_title,
                    "title":current_title,
                    "content": "\n".join(lines_for_title)
                })
                # 清空lines_for_title
                lines_for_title.clear()

            # 连续标题情况
            if current_title and len(lines_for_title) == 1:
                current_title = current_title + "_" + strip_line
                lines_for_title = [current_title]
                continue

            # 判断有没有孤儿数据
            if (not current_title) and len(lines_for_title) > 0:
                temp = [strip_line]
                temp.extend(lines_for_title)
                lines_for_title = temp
            else:
                lines_for_title.append(strip_line)

            current_title = strip_line
        else:
            lines_for_title.append(line)

    if current_title and len(lines_for_title) > 1:
        chunks.append({
            "file_title": file_title,
            "title":current_title,
            "content": "\n".join(lines_for_title)
        })

    logger.info(f"对文档使用标题切分完毕，共切分出来{len(chunks)}块，准备对较长的语义块进行切分")

    return chunks

def _split_long_chunks(chunk:dict[str, Any]) ->list[dict[str, Any]]:
    chunks = []

    content = chunk["content"]
    deal_content = content[len(chunk["title"]) + 1: ]
    prefix = chunk["title"] + "\n"

    splitter = RecursiveCharacterTextSplitter(
        chunk_size = CHUNK_SIZE - len(prefix),
        chunk_overlap = CHUNK_OVERLAP,
        separators=["\n\n", "\n", "。", "；", "？", "！", "，", " "]
    )

    split_chunks = splitter.split_text(deal_content)

    for index, split_chunk in enumerate(split_chunks, start=1):
        chunks.append({
            "file_title": chunk["file_title"],
            "parent_title": chunk["title"],
            "title": f"{chunk['title']}_{index}",
            "part": index,
            "content": prefix + split_chunk
        })
        
    return chunks

def _merge_short_chunks(chunks: list[dict[str, Any]]) ->list[dict[str, Any]]:
    merged_chunks = []

    base_chunk:dict[str, Any] | None = None
    for next_chunk in chunks:
        if not base_chunk:
            base_chunk = next_chunk
            continue

        # 判断是否小于最小长度
        if len(base_chunk["content"]) < CHUNK_MIN:
            # 属于同一个父标题
            if (base_chunk.get("parent_title")) and \
            (base_chunk.get("parent_title") == next_chunk.get("parent_title")):
                next_chunk_content_drop_prefix = next_chunk["content"][len(next_chunk["parent_title"]) + 1: ]
                # 同一个父标题且合并后长度小于1000，合并
                if len(base_chunk["content"]) + len(next_chunk_content_drop_prefix) <= CHUNK_MAX_SIZE:
                    base_chunk["content"] = base_chunk["content"] + "\n" +next_chunk_content_drop_prefix
                # 同一个父标题且合并后长度大于1000，不合并
                else:
                    merged_chunks.append(base_chunk)
                    base_chunk = next_chunk
            # 不属于同一个父标题，不合并
            else:
                merged_chunks.append(base_chunk)
                base_chunk = next_chunk
        # 不小于最小长度，不需要合并
        else:
            merged_chunks.append(base_chunk)
            base_chunk = next_chunk

    if base_chunk:
        merged_chunks.append(base_chunk)

    logger.info(f"对每个语义块中较短的部分合并完毕，合并完后共有{len(merged_chunks)}块")

    return merged_chunks

@step_log("split_and_merge_chunks")
def split_and_merge_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    对经过标题切割后的语义块进行长切分和短合并。

    Args:
        chunks: 经过标题切割后的语义块列表。

    Returns: 
        返回经过长切分和短合并后的语义块列表
    """

    refine_chunks = []

    # 长切分
    for chunk in chunks:
        content = chunk["content"]

        if len(content) > CHUNK_SIZE:
            refine_chunks.extend(_split_long_chunks(chunk))
        else:
            refine_chunks.append(chunk)

    logger.info(f"对每个语义块中较长的部分切割完毕，共切分出来{len(refine_chunks)}块，准备对较短的内容进行合并")

    # 短合并
    merged_chunks = _merge_short_chunks(refine_chunks)

    return merged_chunks

@step_log("padding_metadata_for_chunks")
def padding_metadata_for_chunks(chunks: list[dict[str, Any]]) -> None:
    """
    对切分完成后的chunks填充缺失的参数。
    """

    for chunk in chunks:
        if "parent_title" not in chunk:
            chunk["parent_title"] = chunk["title"]

        if "part" not in chunk:
            chunk["part"] = 1

    logger.info(f"完成对chunks中缺少的字段进行填充")

@step_log("backup_chunks_as_json")
def backup_chunks_as_json(chunks: list[dict[str, Any]], md_path: str) -> None:
    """
    备份切割后的chunks。
    """

    json_file_path = Path(md_path).parent / f"{Path(md_path).stem}.json"

    json_file_path.write_text(data=json.dumps(chunks, ensure_ascii=False, indent=4), encoding="utf-8")

    logger.info(f"已将chunks备份为json文件, 文件地址为: {str(json_file_path)}")

@step_log("split_document")
def split_document(state: ImportGraphState) -> ImportGraphState:
    """
    文档切分服务：
    1. 按标题层级做一级粗切
    2. 对超长文本做二次细切
    3. 构造 chunks 列表
    4. 回写 chunks
    """

    # 检验数据
    md_content, md_path, file_title = validate_get_data(state)

    chunks = split_by_title(md_content, file_title)

    refine_chunks = split_and_merge_chunks(chunks)

    padding_metadata_for_chunks(refine_chunks)

    backup_chunks_as_json(refine_chunks, md_path)

    return state