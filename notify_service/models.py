from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .policy import normalize_headers, normalize_url


Status = Literal["pending", "in_flight", "succeeded", "dead"]


class NotificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(default=None, max_length=65536)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return normalize_url(value)[0]

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return normalize_headers(value)

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 65536:
            raise ValueError("Body exceeds 65536 UTF-8 bytes")
        return value


class NotificationView(BaseModel):
    id: str
    status: Status
    created_at: float
    updated_at: float
    attempts: int
    total_attempts: int
    max_attempts: int
    next_attempt_at: float | None
    lease_until: float | None
    last_http_status: int | None
    last_error: str | None


class AttemptView(BaseModel):
    attempt_no: int
    cycle_attempt_no: int
    started_at: float
    finished_at: float | None
    outcome: str
    http_status: int | None
    error: str | None


class NotificationDetail(NotificationView):
    history: list[AttemptView]


class SubmissionResponse(BaseModel):
    id: str
    status: Status
    deduplicated: bool
