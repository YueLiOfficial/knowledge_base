from pydantic import BaseModel

class StreamRequestSchema(BaseModel):
    session_id: str
    query: str
    is_stream: bool = True

class StreamResponseSchema(BaseModel):
    session_id: str
    message: str

class NotStreamResponseSchema(BaseModel):
    session_id: str
    message: str
    answer: str
    done_list: list[str]
    image_urls: list[str]

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

class SearchHistorySchema(BaseModel):
    session_id: str
    items: list[SearchHistoryItemSchema]
