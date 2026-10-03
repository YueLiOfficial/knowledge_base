from pydantic import BaseModel

class UploadResponseSchema(BaseModel):
    code: int = 200
    msg: str
    task_ids: list[str]

class StatusResponseSchema(BaseModel):
    code: int = 200
    task_id: str
    status: str
    running_list: list[str]
    done_list:list[str]