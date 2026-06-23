from pydantic import BaseModel, ConfigDict


class APIPage(BaseModel):
    page: int
    page_size: int
    total: int


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

