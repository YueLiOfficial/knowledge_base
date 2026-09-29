from app.shared.config.embedding_config import embedding_config, EmbeddingConfig
from app.shared.config.lm_config import lm_config, LLMConfig
from app.shared.config.bailian_mcp_config import mcp_config, McpConfig
from app.shared.config.milvus_config import milvus_config, MilvusConfig
from app.shared.config.mineru_config import mineru_config, MinerUConfig
from app.shared.config.minio_config import minio_config, MinIOConfig
from app.shared.config.reranker_config import reranker_config, RerankerConfig
from app.shared.config.settings_config import settings, AppSettings

from dataclasses import dataclass , field

@dataclass
class InfraConfig:
    embedding_config: EmbeddingConfig = field(default_factory=lambda: embedding_config)
    lm_config: LLMConfig = field(default_factory=lambda: lm_config)
    mcp_config: McpConfig = field(default_factory=lambda: mcp_config)
    milvus_config: MilvusConfig = field(default_factory=lambda: milvus_config)
    mineru_config: MinerUConfig = field(default_factory=lambda: mineru_config)
    minio_config: MinIOConfig = field(default_factory=lambda: minio_config)
    reranker_config: RerankerConfig = field(default_factory=lambda: reranker_config)
    settings: AppSettings = field(default_factory=lambda: settings)

if __name__ == "__main__":
    infra_config = InfraConfig()

    print(infra_config.lm_config.api_key)