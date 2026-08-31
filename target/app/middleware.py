"""A fixed-window rate limiter, applied per client IP to the create endpoint.

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
