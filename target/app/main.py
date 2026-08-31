"""ASGI entry point: `uvicorn app.main:app --reload`."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import storage
from app.routes import router
from app.middleware import RateLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    yield


app = FastAPI(title="URL Shortener", version="1.0", lifespan=lifespan)

app.add_middleware(RateLimitMiddleware)

# Registered before `router`: `/{code}` in routes.py is a catch-all for any
# single path segment, so a literal route added after it would never match --
# Starlette dispatches to the first route whose pattern matches, in
# registration order.
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(router)
