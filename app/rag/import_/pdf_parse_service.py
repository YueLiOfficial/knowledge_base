from app.process.import_.agent.state import ImportGraphState
from typing import Tuple
from pathlib import Path
from app.shared.runtime.logger import logger, PROJECT_ROOT, step_log
import requests
from app.infra.config.providers import infra_config
from app.rag.import_.config import *
import time
import shutil

@step_log("validate_pdf_paths")
def validate_pdf_paths(state: ImportGraphState) -> Tuple[Path, Path]: 
    """
    校验pdf路径和输出文件夹路径。

    Args:
        state: ImportGraphState
    Return: 
        Tuple[pdf_path, local_dir]
    """

    pdf_path = state.get("pdf_path")
    local_dir = state.get("local_dir")

    # 判断输入是否为空
    if not pdf_path:
        logger.error(f"pdf文件路径为空, 请输入正确文件路径!")
        raise ValueError(f"pdf文件路径为空, 请输入正确路径!")

    if not local_dir:
        local_dir = PROJECT_ROOT / "output"
        logger.warning(f"解析后的文件输出目录为空, 默认为{local_dir}")

    # 将输入路径转换为Path
    pdf_path_obj: Path = Path(pdf_path)
    local_dir_obj: Path = Path(local_dir)

    # 判断文件是否存在
    if not pdf_path_obj.is_file():
        logger.error(f"pdf文件不存在, 请输入正确文件路径!")
        raise ValueError(f"pdf文件不存在, 请输入正确路径!")

    # 判断输出文件夹是否存在
    if not local_dir_obj.is_dir():
        logger.warning(f"输出文件夹{local_dir_obj}不存在, 自动创建文件夹")
        local_dir_obj.mkdir(parents=True, exist_ok=True)

    return pdf_path_obj, local_dir_obj

@step_log("upload_pdf_and_poll")
def upload_pdf_and_poll(pdf_path: Path) -> str:
    """
    上传pdf并获取解析解析后的zip文件下载地址。

    Args:
        pdf_path: pdf文件的路径。
    
    Returns: 
        解析后的zip文件下载地址。
    """

    token = infra_config.mineru_config.api_key
    url = f"{infra_config.mineru_config.base_url}/file-urls/batch"
    header = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    data = {
        "files": [
            {"name":f"{pdf_path.stem}.pdf", "data_id": f"{pdf_path.stem}"}
        ],
        "model_version":MINERU_MODEL_VERSION
    }

    # 第一次请求，获取文件上传地址
    response = requests.post(url=url, headers=header, json=data)

    if response.status_code != 200:
        logger.error(f"请求上传地址失败, 错误状态码: {response.status_code}")
        raise RuntimeError(f"请求上传地址失败, 错误状态码: {response.status_code}")

    result = response.json()
    code = result.get("code", -1)
    if code != 0:
        logger.error(f"请求上传地址接口出现错误, 错误码code: {code}, 错误信息: {result.get('msg')}")
        raise RuntimeError(f"请求上传地址接口出现错误, 错误码code: {code}, 错误信息: {result.get('msg')}")

    upload_url = result.get("data", {}).get("file_urls", [])[0]
    batch_id = result.get("data", {}).get("batch_id", None)

    logger.info(f"解析上传地址成功, 等待上传文件")

    # 第二次请求，上传文件
    pdf_bytes = pdf_path.read_bytes()
    with requests.Session() as session:
        session.trust_env = False

        upload_resp = session.put(url=upload_url, data=pdf_bytes)
        if upload_resp.status_code != 200:
            logger.error(f"文件上传请求失败, 错误状态码: {upload_resp.status_code}")
            raise RuntimeError(f"文件上传请求失败, 错误状态码: {upload_resp.status_code}")
        
    logger.info(f"文件上传成功, 等待解析")
    # 文件上传请求成功, 轮询检查是否处理完成
    get_zip_url = f"{infra_config.mineru_config.base_url}/extract-results/batch/{batch_id}"
    start_time = time.time()
    while True:
        if time.time() - start_time > MINERU_POLL_TIMEOUT_SECONDS:
            logger.error(f"文件解析超时!")
            raise TimeoutError(f"文件解析超时!")
        try:
            resp = requests.get(url=get_zip_url, headers=header)
        except Exception:
            logger.warning(f"请求结果失败, 等待重试!")
            time.sleep(MINERU_POLL_INTERVAL_SECONDS)
            continue

        if resp.status_code != 200:
            if 500 <= resp.status_code < 600:
                logger.warning(f"MinerU服务器错误, 等待重试")
                time.sleep(MINERU_POLL_INTERVAL_SECONDS)
                continue
            else:
                logger.error(f"请求结果失败, 状态码: {resp.status_code}")
                raise RuntimeError(f"请求结果失败, 状态码: {resp.status_code}")

        # 请求文件地址成功
        res = resp.json()
        if res.get("code", -1) != 0:
            logger.error(f"请求文件下载地址接口错误, 错误码: {res.get('code', -1)}, 错误信息: {res.get('msg')}")
            raise RuntimeError(f"请求文件下载地址接口错误, 错误码: {res.get('code', -1)}, 错误信息: {res.get('msg')}")

        # 判断文件是否解析完成
        extract_result = res.get("data", {}).get("extract_result", [])[0]
        state = extract_result.get("state")
        
        if state == "done":
            # 文件解析完成
            full_zip_url = extract_result.get("full_zip_url")
            if not full_zip_url:
                logger.error(f"文件解析完成, 但未获得zip文件下载地址")
            logger.info(f"文件解析完成, 文件下载地址: {full_zip_url}")
            return full_zip_url
        
        elif state == "failed":
            logger.error(f"文件解析失败, 错误原因: {extract_result.get('err_msg')}")
            raise RuntimeError(f"文件解析失败, 错误原因: {extract_result.get('err_msg')}")
        else:
            logger.info(f"文件正在解析, 请等待")
            time.sleep(MINERU_POLL_INTERVAL_SECONDS)

@step_log("download_and_extract_markdown")
def download_and_extract_markdown(zip_load_path: str, local_dir: Path, file_name:str) -> Path:
    """
    下载zip文件并解压。

    Args: 
        zip_load_path: zip文件下载地址。
        local_dir: 解压后的文件保存路径。
        file_name: 文件名称。

    Returns: 
        解析后的md文件地址。
    """

    resp = requests.get(url=zip_load_path, timeout=MINERU_DOWNLOAD_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        logger.error(f"下载zip文件请求失败, 错误状态码: {resp.status_code}")
        raise RuntimeError(f"下载zip文件请求失败, 错误状态码: {resp.status_code}")

    # 保存下载好的zip文件
    zip_save_path = local_dir / f"{file_name}.zip"
    zip_save_path.write_bytes(resp.content)

    logger.info(f"文件{zip_save_path.name}下载完毕, 等待解压")

    # 解压
    unpack_dir = local_dir / file_name
    # 如果文件夹存在，则清空，防止旧文件干扰
    if unpack_dir.is_dir():
        shutil.rmtree(unpack_dir)
    
    unpack_dir.mkdir(parents=True, exist_ok=True)

    shutil.unpack_archive(zip_save_path, unpack_dir)

    file_list = list(unpack_dir.rglob("*.md"))

    if not file_list:
        logger.error(f"文件解压成功, 但未发现md文件")
        raise RuntimeError(f"文件解压成功, 但未发现md文件")

    for file in file_list:
        if file.name == f"{file_name}.md":
            return file

    full_md_path = None
    for file in file_list:
        if file.name == "full.md":
            full_md_path = file
            break
    if not full_md_path:
        logger.error(f"解压后的文件中没有找到full.md")
        raise FileNotFoundError(f"解压后的文件中没有找到full.md")

    full_md_path = full_md_path.rename(full_md_path.with_name(f"{file_name}.md"))

    logger.info(f"文件解压完成, md文件的路径为: {full_md_path}")

    return full_md_path

@step_log("parse_pdf_to_markdown")
def parse_pdf_to_markdown(state: ImportGraphState) -> ImportGraphState:
    """
    PDF 解析服务：
    1. 调用 MinerU
    2. 下载并解压解析结果
    3. 获取 Markdown 路径和正文内容
    4. 回写 md_path / md_content / local_dir
    """

    pdf_path_obj, local_dir_obj = validate_pdf_paths(state)

    zip_load_path = upload_pdf_and_poll(pdf_path_obj)

    md_path = download_and_extract_markdown(zip_load_path, local_dir_obj, pdf_path_obj.stem)

    state["md_path"] = str(md_path)

    return state
