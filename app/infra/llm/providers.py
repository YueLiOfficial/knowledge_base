from app.shared.model.lm_utils import get_llm_client
from app.shared.model.embedding_utils import generate_embeddings

class LLMProvider:

    def vision_model(self, vision_model_name: str):
        return get_llm_client(model=vision_model_name)

    def llm_model(self, llm_model_name: str | None = None, json_mode: bool = False):
        return get_llm_client(model=llm_model_name, json_mode=json_mode)

    def generate_embeddings(self, texts: list[str]):
        return generate_embeddings(texts)

llm_prrovider = LLMProvider()