from app.shared.model.lm_utils import get_llm_client
from app.shared.model.embedding_utils import generate_embeddings
from app.shared.model.reranker_utils import get_reranker_model

class LLMProvider:

    def vision_model(self, vision_model_name: str):
        return get_llm_client(model=vision_model_name)

    def llm_model(self, llm_model_name: str | None = None, json_mode: bool = False):
        return get_llm_client(model=llm_model_name, json_mode=json_mode)

    def generate_embeddings(self, texts: list[str]):
        return generate_embeddings(texts)

    def compute_score(self, query_and_answer_pair):
        reranker = get_reranker_model()

        return reranker.compute_score(query_and_answer_pair, normalize=True)

    def compute_tokens_num(self, content: str):
        reranker = get_reranker_model()

        tokenizer = reranker.tokenizer

        token_ids_list = tokenizer.encode(content, add_special_tokens=False)

        return len(token_ids_list)

llm_prrovider = LLMProvider()