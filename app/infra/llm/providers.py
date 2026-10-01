from app.shared.model.lm_utils import get_llm_client

class LLMProvider:

    def vision_model(self, vision_model_name: str):
        return get_llm_client(model=vision_model_name)

    def llm_model(self, llm_model_name: str | None = None, json_mode: bool = False):
        return get_llm_client(model=llm_model_name, json_mode=json_mode)

llm_prrovider = LLMProvider()