"""Pydantic request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, HttpUrl


class CreateUrlRequest(BaseModel):
    long_url: HttpUrl
    custom_alias: str | None = None
    expires_at: datetime | None = None


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
