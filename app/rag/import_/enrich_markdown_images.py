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
from PIL import Image


def _image_vars() -> tuple[int, int, str]:
    """读取装饰图判定阈值（集中读取配置，便于按文档类型调整）。

    Returns:
        tuple[int, int, str]: (最长边阈值px, 文件字节阈值, 无实质内容标记)
    """
    return (
        int(DECORATIVE_IMAGE_MIN_EDGE_PX),
        int(DECORATIVE_IMAGE_MIN_BYTES),
        str(DECORATIVE_IMAGE_SENTINEL),
    )


def _is_decorative_image(image_path: Path) -> tuple[bool, str]:
    """判断图片是否为装饰性元素（页码角标、编号图标、分隔线等）。

    采用两道成本极低的判据：文件大小与像素尺寸，任一项低于阈值即判为装饰图。
    读不出尺寸时保守返回 False（保留图片），避免误删有效插图。

    Args:
        image_path: 图片文件路径。

    Returns:
        tuple[bool, str]: (是否装饰图, 原因说明)
    """
    min_edge, min_bytes, _ = _image_vars()

    try:
        size_bytes = image_path.stat().st_size
    except OSError as e:
        logger.warning(f"{image_path.name} 无法读取文件信息, 保守保留: {e}")
        return False, ""

    if size_bytes < min_bytes:
        return True, f"文件仅 {size_bytes / 1024:.1f}KB (< {min_bytes / 1024:.0f}KB)"

    try:
        with Image.open(image_path) as im:
            width, height = im.size
    except Exception as e:
        logger.warning(f"{image_path.name} 无法读取图片尺寸, 保守保留: {e}")
        return False, ""

    longest_edge = max(width, height)
    if longest_edge < min_edge:
        return True, f"尺寸仅 {width}x{height} (最长边 < {min_edge}px)"

    return False, ""


def _image_reference_pattern(image_name: str) -> re.Pattern[str]:
    """构造匹配某个图片在 Markdown 中引用（含可选 title）的正则。

    Args:
        image_name: 图片文件名。

    Returns:
        re.Pattern[str]: 匹配 `![alt](url)` 的正则，url 中须包含该文件名。
    """
    return re.compile(r"!\[[^\]]*\]\([^)]*" + re.escape(image_name) + r"[^)]*\)")


def _strip_image_references(md_content: str, removed_names: set[str]) -> str:
    """从 Markdown 中删除指定图片的引用（用于装饰图）。

    Args:
        md_content: Markdown 文本。
        removed_names: 需要移除的图片文件名集合。

    Returns:
        str: 移除后的 Markdown 文本。
    """
    for image_name in removed_names:
        md_content = _image_reference_pattern(image_name).sub("", md_content)
    return md_content


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
def scan_images(md_content: str, images_dir_path: Path) -> tuple[list[tuple[str, Path, tuple[str, str]]], set[str]]:
    """过滤图片文件夹中的图片并截取图片上下文。

    会在扫描阶段丢弃装饰性图片（尺寸/大小过小），并记录其文件名，
    便于后续把它们从 Markdown 中一并移除。

    Args:
        md_content: md文档的内容。
        images_dir_path: 图片文件夹路径。

    Returns:
        tuple[list[tuple[str, Path, tuple[str, str]]], set[str]]:
            (有效图片信息列表, 被判定为装饰图的文件名集合)
            图片信息列表元素为 (image_name, image_path, (pre_content, post_content))
    """

    images_info_list = []
    decorative_names: set[str] = set()

    for image_file in images_dir_path.iterdir():
        if image_file.suffix not in SUPPORTED_IMAGE_EXTENSIONS:
            logger.info(f"{image_file.name}不是图片, 跳过处理!")
            continue

        # 检查图片在md文件中有没有被使用 ![]()
        image_match_obj = _image_reference_pattern(image_file.name).search(md_content)
        if not image_match_obj:
            logger.info(f"{image_file.name}未被md文件使用, 跳过处理")
            continue

        # 装饰性图片（页码角标/编号图标等）直接丢弃，不进入摘要与上传流程
        is_decorative, reason = _is_decorative_image(image_file)
        if is_decorative:
            decorative_names.add(image_file.name)
            logger.info(f"{image_file.name}判定为装饰图并丢弃: {reason}")
            continue

        logger.debug(f"{image_file.name}在md文件中的位置为: {image_match_obj.span()}")

        # 截取前后文
        pre_content = md_content[max(0, image_match_obj.start() - IMAGE_CONTENT_LEN): image_match_obj.start()]
        post_content = md_content[image_match_obj.end(): min(len(md_content), image_match_obj.end() + IMAGE_CONTENT_LEN)]

        images_info_list.append((image_file.name, image_file, (pre_content, post_content)))
        logger.debug(f"{image_file.name}处理完毕")

    if decorative_names:
        logger.info(f"共丢弃 {len(decorative_names)} 张装饰图, 保留 {len(images_info_list)} 张有效图片")

    return images_info_list, decorative_names

@step_log("summary_images")
def summary_images(images_info_list: list[tuple[str, Path, tuple[str, str]]], root_folder: str) -> tuple[dict[str, str], set[str]]:
    """对图片内容进行识别。

    视觉模型被要求在图片无实质内容时只回复 DECORATIVE_IMAGE_SENTINEL，
    此类图片会被丢弃（不生成摘要、后续不上传、并从 Markdown 中移除）。

    Args:
        images_info_list: 图片信息列表, [(str, Path, (str, str))].
        root_folder: 图片所在文件夹。

    Returns:
        tuple[dict[str, str], set[str]]: (图片摘要字典 {image_name: summary}, 被模型判为无内容的文件名集合)
    """
    
    image_summary_dict = {}
    decorative_names: set[str] = set()
    _, _, sentinel = _image_vars()
    sentinel_key = sentinel.strip().upper()
    
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

        try:
            image_summary = chains.invoke(message)
        except Exception as e:
            # 单张图片识别失败不应中断整篇文档的导入
            logger.warning(f"{image_name} 摘要识别失败, 跳过该图片: {e}")
            decorative_names.add(image_name)
            continue

        # 模型判定为装饰元素：丢弃，且不写入 Markdown
        if sentinel_key and sentinel_key in str(image_summary).strip().upper():
            logger.info(f"{image_name}被模型判定为无实质内容, 丢弃")
            decorative_names.add(image_name)
            continue

        image_summary_dict[image_name] = image_summary

        logger.debug(f"{image_name}的含义识别完成, 含义为: {image_summary}")

    return image_summary_dict, decorative_names

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
                              images_url_dict: dict[str, str]):
    """
    替换md_content中的图片地址。

    使用闭包默认参数逐张替换，避免匿名函数捕获循环变量导致的错配：
    原实现用 lambda _ 忽略匹配内容并引用循环变量，会让多张图片被反复替换成
    同一个摘要与 URL（表现为摘要覆盖错误、URL 重复）。

    Args:
        md_content: md文本内容。
        images_summary_dict: 图片摘要字典。
        images_url_dict: 图片url字典。
    
    Returns:
        替换后的md_content。
    """
    
    for image_name, image_summary in images_summary_dict.items():
        image_url = images_url_dict.get(image_name)

        # 上传失败（拿不到 URL）时保留原始引用，避免写出 ![](None) 这种坏链接
        if not image_url:
            logger.warning(f"{image_name} 没有可用的图片URL, 保留原始引用")
            continue

        def _replacement(match, name: str = image_name, summary: str = image_summary, url: str = image_url):
            return f"![{summary}]({url})"

        # 替换内容中含 / 会被正则误识别，因此用回调返回字符串，正则不解析返回值
        md_content = _image_reference_pattern(image_name).sub(_replacement, md_content)

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
    1. 扫描 Markdown 中的图片，并按尺寸/大小丢弃装饰图
    2. 调用多模态模型生成图片说明，并按模型判定丢弃无实质内容的图片
    3. 上传有效图片到 MinIO
    4. 替换 Markdown 图片地址，并移除被丢弃图片的引用
    """

    md_path, images_dir_path, md_content = load_markdown_and_image_dir(state)

    if (not images_dir_path.is_dir()) or (not any(images_dir_path.iterdir())):
        logger.info(f"此md文件中没有检测到图片")
        return state

    # 第一道过滤：尺寸/文件大小（装饰图不进入摘要与上传流程）
    images_info_list, size_filtered_names = scan_images(md_content, images_dir_path)

    # 第二道过滤：视觉模型判定图片是否包含实质内容
    image_summary_dict, model_filtered_names = summary_images(images_info_list, md_path.stem)

    # 只上传通过两道过滤的图片
    uploaded_names = set(image_summary_dict.keys())
    keep_list = [info for info in images_info_list if info[0] in uploaded_names]
    image_url_dict = upload_images(keep_list, md_path.stem)

    removed_names = size_filtered_names | model_filtered_names

    # 先按摘要/URL 替换有效图片，再删除装饰图的引用
    new_md_content = replace_md_content_images(md_content, image_summary_dict, image_url_dict)
    new_md_content = _strip_image_references(new_md_content, removed_names)

    total_scanned = len(images_info_list) + len(size_filtered_names)
    logger.info(
        f"图片处理完成: 扫描 {total_scanned} 张, 保留 {len(uploaded_names)} 张, "
        f"丢弃 {len(removed_names)} 张 (尺寸过滤 {len(size_filtered_names)}, 模型判定 {len(model_filtered_names)})"
    )

    state["md_content"] = new_md_content

    new_md_path = backup_new_md_content(new_md_content, md_path)

    state["md_path"] = str(new_md_path)

    return state