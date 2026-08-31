"""Pydantic request/response schemas."""

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field, HttpUrl


class CreateUrlRequest(BaseModel):
    long_url: HttpUrl
    custom_alias: str | None = None
    expires_at: datetime | None = Field(
        default=None,
        examples=[(datetime.now(UTC) + timedelta(hours=24)).isoformat()],
    )


class UrlResponse(BaseModel):
    code: str
    short_url: str
    long_url: str
    created_at: datetime
    expires_at: datetime | None = None


class StatsResponse(BaseModel):
    code: str
    click_count: int
    last_accessed_at: datetime | None = None
