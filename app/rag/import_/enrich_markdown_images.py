from app.process.import_.agent.state import ImportGraphState
from app.shared.runtime.logger import logger, step_log
from pathlib import Path
from app.rag.import_.config import *
import re
from app.infra.llm.providers import llm_prrovider
from app.infra.config.providers import infra_config
from app.shared.runtime.load_prompt import load_prompt
from langchain.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser
import base64
from mimetypes import guess_type
from app.shared.utils.rate_limit_utils import apply_api_rate_limit
from app.infra.object_storage.minio_gateway import minio_gateway
from minio.deleteobjects import DeleteObject

@step_log("load_markdown_and_image_dir")
def load_markdown_and_image_dir(state: ImportGraphState) -> tuple[Path, Path, str]:
    """
    加载md文档内容和文档图片。

    Args:
        state: ImportGraphState.
    
    Returns:
        Tuple[md_path, images_dir_path, md_content]
    """

    md_path = state.get("md_path")
    if not md_path:
        logger.error(f"md文件路径为空, 请输入正确路径!")
        raise ValueError(f"md文件路径为空, 请输入正确路径!")

    md_path_obj = Path(md_path)
    if not md_path_obj.is_file():
        logger.error(f"md文件不存在, 请输入正确文件路径!")
        raise FileNotFoundError(f"md文件不存在, 请输入正确文件路径!")

    md_content = md_path_obj.read_text(encoding="utf-8")
    state["md_content"] = md_content

    images_dir_path = md_path_obj.parent / "images"

    return md_path_obj, images_dir_path, md_content

@step_log("scan_images")
def scan_images(md_content:str, images_dir_path: Path) -> list[tuple[str, Path, tuple[str, str]]]:
    """
    过滤图片文件夹中的图片并截取图片上下文。

    Args:
        md_content: md文档的内容。
        images_dir_path: 图片文件夹路径。

    Returns:
        List[Tuple[image_name, image_path, Tuple[pre_content, post_content]]]
    """

    images_info_list = []

    for image_file in images_dir_path.iterdir():
        if image_file.suffix not in SUPPORTED_IMAGE_EXTENSIONS:
            logger.info(f"{image_file.name}不是图片, 跳过处理!")
            continue
        
        # 检查图片在md文件中有没有被使用 ![]()
        reg = re.compile(r"\!\[.*?\]\(.*?"+re.escape(image_file.name)+r".*?\)")

        image_match_obj = reg.search(md_content)
        if not image_match_obj:
            logger.info(f"{image_file.name}未被md文件使用, 跳过处理")
        else:
            logger.debug(f"{image_file.name}在md文件中的位置为: {image_match_obj.span()}")

            # 截取前后文
            pre_content = md_content[max(0, image_match_obj.start() - IMAGE_CONTENT_LEN): image_match_obj.start()]
            post_content = md_content[image_match_obj.end(): min(len(md_content), image_match_obj.end() + IMAGE_CONTENT_LEN)]

            images_info_list.append((image_file.name, image_file, (pre_content, post_content)))
            logger.debug(f"{image_file.name}处理完毕")

    return images_info_list

@step_log("summary_images")
def summary_images(images_info_list: list[tuple[str, Path, tuple[str, str]]], root_folder: str) -> dict[str, str]:
    """
    对图片内容进行识别。

    Args: 
        images_info_list: 图片信息列表, [(str, Path (str, str))].
        root_dir: 图片所在文件夹。

    Returns:
        图片摘要字典, {image_name: image_summary}.
    """
    
    image_summary_dict = {}
    
    vision_model = llm_prrovider.vision_model(infra_config.lm_config.lv_model)
    
    for image_name, image_path, image_content in images_info_list:
        # 拼prompt
        prompt = load_prompt("image_summary", root_folder=root_folder, image_content=image_content)

        image_base64_content = base64.b64encode(image_path.read_bytes()).decode("utf-8")

        message = [HumanMessage(
            content=[
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": f"data:{guess_type(image_name)[0]};base64, {image_base64_content}"}
            ]
        )]

        apply_api_rate_limit()
        chains = vision_model | StrOutputParser()

        image_summary = chains.invoke(message)

        image_summary_dict[image_name] = image_summary

        logger.debug(f"{image_name}的含义识别完成, 含义为: {image_summary}")

    return image_summary_dict

@step_log("upload_images")
def upload_images(images_info_list: list[tuple[str, Path, tuple[str, str]]], stem: str) -> dict[str, str]:
    """
    将图片上传到MinIO服务器, 并获取图片的url。

    Args:
        images_info_list: 图片信息列表, [(str, Path (str, str))].
        root_dir: 图片所在文件夹。

    Returns:
        图片url字典, {image_name: image_url}.
    """
    
    images_url_dict = {}

    minio_client = minio_gateway.minio_client

    # 清空桶中内容
    objs = minio_client.list_objects(bucket_name=minio_gateway.bucket_name, 
                                     # prefix 不能使用 / 开头
                                     prefix=minio_gateway.img_dir[1:] + "/" + stem, 
                                     recursive=True)
    if objs:
        delete_obj_list = [DeleteObject(obj.object_name) for obj in objs if obj.object_name]

        errors = minio_client.remove_objects(minio_gateway.bucket_name, delete_obj_list)

        for error in errors:
            logger.warning(f"删除图片出现问题: {error}")

    for image_name, image_path, _ in images_info_list:
        try:
            minio_client.fput_object(
                bucket_name=infra_config.minio_config.bucket_name,
                object_name=infra_config.minio_config.minio_img_dir + "/" + stem + "/" + image_name,
                file_path=str(image_path),
                content_type=guess_type(image_name)[0] # type: ignore
            )

            url = minio_gateway.build_image_url(stem, image_name)

            images_url_dict[image_name] = url

            logger.debug(f"{image_name}上传成功, url: {url}")
        except Exception as e:
            logger.warning(f"{image_name}上传失败, 跳过此图片!")

    return images_url_dict

@step_log("replace_md_content_images")
def replace_md_content_images(md_content: str, 
                              images_summary_dict: dict[str, str], 
                              images_url_dict: dict[str, str]
):
    """
    替换md_content中的图片地址。

    Args:
        md_content: md文本内容。
        images_summary_dict: 图片摘要字典。
        images_url_dict: 图片url字典。
    
    Returns:
        替换后的md_content。
    """
    
    for image_name, image_summary in images_summary_dict.items():
        image_url = images_url_dict.get(image_name)

        reg = re.compile(r"\!\[.*?\]\(.*?"+re.escape(image_name)+r".*?\)")

        # 替换的内容中有 / 会被正则识别，因此可以调用匿名函数返回要替换的字符串，正则不会检查返回值
        md_content = reg.sub(lambda _: f"![{image_summary}]({image_url})", md_content)

        logger.debug(f"{image_name}已经成功替换")

    return md_content

@step_log("backup_new_md_content")
def backup_new_md_content(new_md_content: str, md_path: Path) -> Path:
    """
    备份新的md_content。

    Args:
        new_md_content: 替换后的新的md_content。
        md_path: md文件保存路径
    
    Returns:
        新的md文件路径。
    """

    new_md_path = md_path.parent / f"{md_path.stem}_new.md"

    new_md_path.write_text(new_md_content, encoding="utf-8")

    logger.info(f"已备份新的md文件, 路径为 {new_md_path}")

    return new_md_path

@step_log("enrich_markdown_images")
def enrich_markdown_images(state: ImportGraphState) -> ImportGraphState:
    """
    Markdown 图片增强服务：
    1. 扫描 Markdown 中的图片
    2. 调用多模态模型生成图片说明
    3. 上传图片到 MinIO
    4. 替换 Markdown 图片地址并回写 md_content
    """

    md_path, images_dir_path, md_content = load_markdown_and_image_dir(state)

    if (not images_dir_path.is_dir()) or (not any(images_dir_path.iterdir())):
        logger.info(f"此md文件中没有检测到图片")
        return state

    images_info_list = scan_images(md_content, images_dir_path)

    image_summary_dict = summary_images(images_info_list, md_path.stem)

    image_url_dict = upload_images(images_info_list, md_path.stem)

    new_md_content = replace_md_content_images(md_content, image_summary_dict, image_url_dict)

    state["md_content"] = new_md_content

    new_md_path = backup_new_md_content(new_md_content, md_path)

    state["md_path"] = str(new_md_path)

    return state