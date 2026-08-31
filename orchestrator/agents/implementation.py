"""Stage 4: Implementation.

Generates the URL shortener's actual source: a FastAPI app over a SQLite
repository, base62 short codes, custom aliases, expiration, click analytics
and an in-memory rate limiter -- gated by which optional tasks planning
decided the requirement calls for, so a narrow brownfield change produces a
narrow diff instead of regenerating the whole service.

The generator is deterministic (plain string templates against a small
in-memory domain model), not a live model call: implementation is exactly
the stage where non-determinism is least welcome -- a reviewer needs the same
input to produce the same diff every time. Templates are plain (non-f)
strings with a handful of `__TOKEN__` substitutions, deliberately: the
generated code is itself full of `{}` (f-strings, dict literals, route
placeholders), and an f-string template would need every one of those braces
escaped -- a token replace is what keeps that from being a silent
generation bug waiting to happen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orchestrator.agents.base import Agent
from orchestrator.contracts import ArtifactKind

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState


class ImplementationAgent(Agent):
    stage_name = "implementation"

    async def run(self, node: StageNode, state: RunState) -> object:
        raw_plan = state.context.get("plan", reader=self.stage_name)
        task_titles = {t["title"] for t in raw_plan["tasks"]} if raw_plan else set()

        caps = {
            "alias": "custom alias handling" in task_titles,
            "expiry": "expiration handling" in task_titles,
            "stats": "stats endpoint" in task_titles,
            "rate_limit": "rate limiting middleware" in task_titles,
        }

        artifacts = [
            self.artifact("app/__init__.py", ArtifactKind.CODE, ""),
            self.artifact("app/config.py", ArtifactKind.CODE, _config_py()),
            self.artifact("app/codec.py", ArtifactKind.CODE, _codec_py()),
            self.artifact("app/storage.py", ArtifactKind.CODE, _storage_py(caps)),
            self.artifact("app/models.py", ArtifactKind.CODE, _models_py(caps)),
            self.artifact("app/routes.py", ArtifactKind.CODE, _routes_py(caps)),
        ]
        if caps["rate_limit"]:
            artifacts.append(
                self.artifact("app/middleware.py", ArtifactKind.CODE, _middleware_py())
            )
        artifacts.append(self.artifact("app/main.py", ArtifactKind.CODE, _main_py(caps)))
        artifacts.append(
            self.artifact("requirements.txt", ArtifactKind.CONFIG, _requirements_txt())
        )

        decision = self.decision(
            "which optional capabilities are implemented?",
            ", ".join(f"{k}={v}" for k, v in caps.items()),
            "gated directly by the planning stage's task list, so implementation stays in "
            "lockstep with what was actually decomposed rather than a separately-maintained "
            "feature flag",
            confidence=0.9,
        )

        return self.result(
            summary=f"generated {len(artifacts)} source file(s) for the URL shortener service",
            artifacts=tuple(artifacts),
            decisions=(decision,),
            context={"code": {"files": [a.path for a in artifacts], "capabilities": caps}},
        )


def _requirements_txt() -> str:
    return """fastapi>=0.110
uvicorn[standard]>=0.29
pydantic>=2.6
"""


def _config_py() -> str:
    return '''"""Runtime settings, sourced from the environment with safe local defaults."""

import os

DATABASE_PATH = os.environ.get("SHORTENER_DB_PATH", "shortener.db")
BASE_URL = os.environ.get("SHORTENER_BASE_URL", "http://localhost:8000")
RATE_LIMIT_PER_MINUTE = int(os.environ.get("SHORTENER_RATE_LIMIT_PER_MINUTE", "30"))
MIN_CODE_LENGTH = 6
'''


def _codec_py() -> str:
    return '''"""Base62 short-code generation.

A monotonic row id is encoded rather than drawing a random token: it makes
every code unique by construction, so the hot create path never needs a
collision-retry loop. Codes are offset by a fixed power of the base so early
ids ("1", "2", ...) still decode to a minimum length instead of looking
suspiciously short next to later ones -- the same trick as zero-padding in
base 10, where offsetting by 10**(n-1) is what keeps a 6-digit field from
ever showing fewer than 6 digits.
"""

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE = len(_ALPHABET)


def encode(n: int, min_length: int = 6) -> str:
    if n < 0:
        raise ValueError("cannot encode a negative id")
    offset = n + _BASE ** (min_length - 1)
    digits = []
    while offset:
        offset, rem = divmod(offset, _BASE)
        digits.append(_ALPHABET[rem])
    return "".join(reversed(digits))


def decode(code: str, min_length: int = 6) -> int:
    n = 0
    for ch in code:
        n = n * _BASE + _ALPHABET.index(ch)
    return n - _BASE ** (min_length - 1)
'''


def _storage_py(caps: dict[str, bool]) -> str:
    extra_columns = ""
    if caps["expiry"]:
        extra_columns += ",\n                expires_at TEXT"
    if caps["alias"]:
        extra_columns += ",\n                is_custom_alias INTEGER NOT NULL DEFAULT 0"

    template = '''"""SQLite repository for shortened URLs.

A single narrow interface (this module's free functions) is what the rest of
the app depends on -- routes never touch SQL directly. That is the seam a
future swap to Postgres would go through without touching route handlers.
"""

import sqlite3
import threading
from contextlib import contextmanager

from app.config import DATABASE_PATH

_local = threading.local()


def _connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DATABASE_PATH)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


@contextmanager
def cursor():
    conn = _connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def init_db() -> None:
    with cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                long_url TEXT NOT NULL,
                created_at TEXT NOT NULL__EXTRA_COLUMNS__,
                click_count INTEGER NOT NULL DEFAULT 0,
                last_accessed_at TEXT
            )
            """
        )


def insert(code: str, long_url: str, created_at: str, **extra) -> int:
    """Insert a row. `code` may be empty for a row whose code is derived from
    its own id after insertion (see `assign_generated_code`)."""
    columns = ["code", "long_url", "created_at", *extra.keys()]
    placeholders = ", ".join("?" for _ in columns)
    values = [code, long_url, created_at, *extra.values()]
    with cursor() as cur:
        cur.execute(
            f"INSERT INTO urls ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        return cur.lastrowid


def assign_generated_code(row_id: int, code: str) -> None:
    with cursor() as cur:
        cur.execute("UPDATE urls SET code = ? WHERE id = ?", (code, row_id))


def get_by_code(code: str) -> sqlite3.Row | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM urls WHERE code = ?", (code,))
        return cur.fetchone()


def code_exists(code: str) -> bool:
    return get_by_code(code) is not None


def record_click(code: str, accessed_at: str) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE urls SET click_count = click_count + 1, last_accessed_at = ? WHERE code = ?",
            (accessed_at, code),
        )


def delete(code: str) -> bool:
    with cursor() as cur:
        cur.execute("DELETE FROM urls WHERE code = ?", (code,))
        return cur.rowcount > 0
'''
    return template.replace("__EXTRA_COLUMNS__", extra_columns)


def _models_py(caps: dict[str, bool]) -> str:
    alias_field = "\n    custom_alias: str | None = None" if caps["alias"] else ""
    expiry_request_field = "\n    expires_at: datetime | None = None" if caps["expiry"] else ""
    expiry_response_field = "\n    expires_at: datetime | None = None" if caps["expiry"] else ""
    stats_fields = (
        "\n    click_count: int\n    last_accessed_at: datetime | None = None"
        if caps["stats"]
        else ""
    )

    template = '''"""Pydantic request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, HttpUrl


class CreateUrlRequest(BaseModel):
    long_url: HttpUrl__ALIAS_FIELD____EXPIRY_REQUEST_FIELD__


class UrlResponse(BaseModel):
    code: str
    short_url: str
    long_url: str
    created_at: datetime__EXPIRY_RESPONSE_FIELD__


class StatsResponse(BaseModel):
    code: str__STATS_FIELDS__
'''
    return (
        template.replace("__ALIAS_FIELD__", alias_field)
        .replace("__EXPIRY_REQUEST_FIELD__", expiry_request_field)
        .replace("__EXPIRY_RESPONSE_FIELD__", expiry_response_field)
        .replace("__STATS_FIELDS__", stats_fields)
    )


def _routes_py(caps: dict[str, bool]) -> str:
    expiry_extra_kw = (
        ', expires_at=payload.expires_at.isoformat() if payload.expires_at else None'
        if caps["expiry"]
        else ""
    )

    if caps["alias"]:
        create_body = '''
    if payload.custom_alias:
        if storage.code_exists(payload.custom_alias):
            raise HTTPException(status_code=409, detail="alias already in use")
        code = payload.custom_alias
        storage.insert(code, str(payload.long_url), now, is_custom_alias=1__EXTRA_KW__)
    else:
        row_id = storage.insert("", str(payload.long_url), now, is_custom_alias=0__EXTRA_KW__)
        code = codec.encode(row_id, MIN_CODE_LENGTH)
        storage.assign_generated_code(row_id, code)
'''
    else:
        create_body = '''
    row_id = storage.insert("", str(payload.long_url), now__EXTRA_KW__)
    code = codec.encode(row_id, MIN_CODE_LENGTH)
    storage.assign_generated_code(row_id, code)
'''
    create_body = create_body.replace("__EXTRA_KW__", expiry_extra_kw)

    expiry_check = (
        '''
    if row["expires_at"] and row["expires_at"] < _utcnow_iso():
        raise HTTPException(status_code=410, detail="this link has expired")
'''
        if caps["expiry"]
        else ""
    )

    stats_route = (
        '''

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
'''
        if caps["stats"]
        else ""
    )

    template = '''"""HTTP layer: request validation lives in `models`, persistence in `storage`,
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
__CREATE_BODY__
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
__EXPIRY_CHECK__
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
__STATS_ROUTE__'''
    return (
        template.replace("__CREATE_BODY__", create_body)
        .replace("__EXPIRY_CHECK__", expiry_check)
        .replace("__STATS_ROUTE__", stats_route)
    )


def _middleware_py() -> str:
    return '''"""A fixed-window rate limiter, applied per client IP to the create endpoint.

Prototype-grade by design: state is an in-process dict, so it resets on
restart and is not shared across multiple app instances. Documented as a
known limitation (see docs/risk_register.md) rather than silently assumed
away -- a distributed deployment needs a shared store (e.g. Redis) instead.
"""

import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import RATE_LIMIT_PER_MINUTE

_WINDOW_SECONDS = 60


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app) -> None:
        super().__init__(app)
        self._hits: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        if request.method == "POST" and request.url.path == "/api/urls":
            client = request.client.host if request.client else "unknown"
            now = time.time()
            hits = self._hits[client]
            hits[:] = [t for t in hits if now - t < _WINDOW_SECONDS]
            if len(hits) >= RATE_LIMIT_PER_MINUTE:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "rate limit exceeded, try again shortly"},
                )
            hits.append(now)
        return await call_next(request)
'''


def _main_py(caps: dict[str, bool]) -> str:
    middleware_import = (
        "\nfrom app.middleware import RateLimitMiddleware" if caps["rate_limit"] else ""
    )
    middleware_add = "\napp.add_middleware(RateLimitMiddleware)" if caps["rate_limit"] else ""

    template = '''"""ASGI entry point: `uvicorn app.main:app --reload`."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import storage
from app.routes import router__MIDDLEWARE_IMPORT__


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    yield


app = FastAPI(title="URL Shortener", version="1.0", lifespan=lifespan)
__MIDDLEWARE_ADD__

# Registered before `router`: `/{code}` in routes.py is a catch-all for any
# single path segment, so a literal route added after it would never match --
# Starlette dispatches to the first route whose pattern matches, in
# registration order.
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(router)
'''
    return template.replace("__MIDDLEWARE_IMPORT__", middleware_import).replace(
        "__MIDDLEWARE_ADD__", middleware_add
    )
