"""HTTP layer: request validation lives in `models`, persistence in `storage`,
short-code generation in `codec`. This module wires them together and is
kept free of business logic that belongs in one of those.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app import codec, storage
from app.config import BASE_URL, MIN_CODE_LENGTH
from app.models import CreateUrlRequest, StatsResponse, UrlResponse

router = APIRouter()


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


@router.post("/api/urls", response_model=UrlResponse, status_code=201)
def create_url(payload: CreateUrlRequest) -> UrlResponse:
    now = _utcnow_iso()

    if payload.custom_alias:
        if storage.code_exists(payload.custom_alias):
            raise HTTPException(status_code=409, detail="alias already in use")
        code = payload.custom_alias
        storage.insert(code, str(payload.long_url), now, is_custom_alias=1, expires_at=payload.expires_at.isoformat() if payload.expires_at else None)
    else:
        row_id = storage.insert("", str(payload.long_url), now, is_custom_alias=0, expires_at=payload.expires_at.isoformat() if payload.expires_at else None)
        code = codec.encode(row_id, MIN_CODE_LENGTH)
        storage.assign_generated_code(row_id, code)

    row = storage.get_by_code(code)
    return UrlResponse(
        code=code,
        short_url=f"{BASE_URL}/{code}",
        long_url=str(payload.long_url),
        created_at=row["created_at"],
    )


@router.get("/{code}")
def redirect(code: str) -> RedirectResponse:
    row = storage.get_by_code(code)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown code")

    if row["expires_at"] and row["expires_at"] < _utcnow_iso():
        raise HTTPException(status_code=410, detail="this link has expired")

    storage.record_click(code, _utcnow_iso())
    return RedirectResponse(url=row["long_url"], status_code=302)


@router.get("/api/urls/{code}", response_model=UrlResponse)
def get_metadata(code: str) -> UrlResponse:
    row = storage.get_by_code(code)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown code")
    return UrlResponse(
        code=code,
        short_url=f"{BASE_URL}/{code}",
        long_url=row["long_url"],
        created_at=row["created_at"],
    )


@router.delete("/api/urls/{code}", status_code=204)
def delete_url(code: str) -> None:
    if not storage.delete(code):
        raise HTTPException(status_code=404, detail="unknown code")


@router.get("/api/urls/{code}/stats", response_model=StatsResponse)
def get_stats(code: str) -> StatsResponse:
    row = storage.get_by_code(code)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown code")
    return StatsResponse(
        code=code,
        click_count=row["click_count"],
        last_accessed_at=row["last_accessed_at"],
    )
