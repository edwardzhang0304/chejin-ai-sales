from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    username: str = Field(max_length=128)
    password: str = Field(max_length=1024)
