from app.infra.config.providers import infra_config
from app.shared.clients.minio_utils import get_minio_client

class MinIOGateway:

    # 获取桶的名字
    @property
    def bucket_name(self):
        return infra_config.minio_config.bucket_name
    # 获取文件前缀
    @property
    def img_dir(self):
        return infra_config.minio_config.minio_img_dir
    # 获取minio客户端
    @property
    def minio_client(self):
        return get_minio_client()
    # 拼接文件地址
    def build_image_url(self, file_name: str, object_name: str):
        prefix = "https://" if infra_config.minio_config.minio_secure else "http://"

        url = prefix + infra_config.minio_config.endpoint + "/" + infra_config.minio_config.bucket_name \
        + infra_config.minio_config.minio_img_dir + "/" + file_name + "/" + object_name

        return url

minio_gateway = MinIOGateway()