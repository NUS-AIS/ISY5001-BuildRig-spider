from typing import Any, Literal
from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    locale: str = "zh-CN"
    market: str = "SG"
    currency: str = "SGD"
    long_term_memory: bool = False


class MessageCreate(BaseModel):
    client_message_id: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=8000)
    expected_requirements_version: int = Field(ge=0)
    reply_to_question_id: str | None = None
    context: dict[str, str] | None = None


class RunCreate(BaseModel):
    requirements_version: int = Field(ge=1)
    orchestration_mode: Literal["dag", "pi"] = "dag"
    maximum_options: int = Field(default=3, ge=1, le=5)
    base_run_id: str | None = None


class MemoryPatch(BaseModel):
    expected_version: int = Field(ge=1)
    value: Any
    confirmed: bool = True


class HybridRequest(BaseModel):
    run_id: str
    query: str = Field(min_length=1, max_length=4000)
    snapshot_id: str
    product_ids: list[str] = []
    categories: list[str] = []
    top_k: int = Field(default=8, ge=1, le=50)


class ValidationRequest(BaseModel):
    run_id: str
    option: dict[str, Any]
    requirements: dict[str, Any]
