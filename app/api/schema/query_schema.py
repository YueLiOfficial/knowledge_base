from pydantic import BaseModel

class StreamRequestSchema(BaseModel):
    session_id: str
    query: str
    is_stream: bool = True
    # 直选语义：用户已在候选卡片上明确选定商品，直接以该名称作为 item_names，
    # 跳过 node_item_name_confirm 的 LLM 抽取与向量检索。默认空表示按原有流程处理。
    force_item_names: list[str] = []

class StreamResponseSchema(BaseModel):
    session_id: str
    message: str

class NotStreamResponseSchema(BaseModel):
    session_id: str
    message: str
    answer: str
    done_list: list[str]
    image_urls: list[str]
    option_item_names: list[str] = []  # 待用户确认的候选商品名称

class ClearHistorySchema(BaseModel):
    message: str
    delete_count: int

class SearchHistoryItemSchema(BaseModel):
    id: str
    session_id: str
    role: str
    text: str
    rewritten_query: str
    item_names: list[str]
    image_urls: list[str]
    ts: float
    option_item_names: list[str] = []  # 待用户确认的候选商品名称

class SearchHistorySchema(BaseModel):
    session_id: str
    items: list[SearchHistoryItemSchema]
