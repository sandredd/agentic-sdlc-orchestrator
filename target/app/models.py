"""Pydantic request/response schemas."""

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field, field_validator, HttpUrl


class CreateUrlRequest(BaseModel):
    long_url: HttpUrl
    custom_alias: str | None = None
    expires_at: datetime | None = Field(
        default=None,
        examples=[(datetime.now(UTC) + timedelta(hours=24)).isoformat()],
    )

    @field_validator("expires_at", mode="before")
    @classmethod
    def _blank_means_absent(cls, value):
        # A browser form (e.g. Swagger UI's "Try it out") that leaves
        # an untouched optional field submits "" rather than omitting
        # the key entirely; Pydantic's datetime parser rejects that
        # outright ("input is too short") instead of treating it the
        # same as the field never having been provided. Normalizing
        # here keeps both cases meaning the same thing: never expires.
        return None if value == "" else value


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
