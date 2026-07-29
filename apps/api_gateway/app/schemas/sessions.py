from pydantic import BaseModel


class BulkDeleteRequest(BaseModel):
    ids: list[str] = []
