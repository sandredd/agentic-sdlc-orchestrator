"""ASGI entry point: `uvicorn app.main:app --reload`."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import storage
from app.routes import router
from app.middleware import RateLimitMiddleware


def _print_routes(app: FastAPI) -> None:
    """Most of this API isn't clickable in a browser -- POST/DELETE need a
    body, and GET routes with a {code} need one to already exist -- so
    printing the route table at startup is the difference between a
    developer finding it in this log and one giving up on a bare 404 at `/`.

    Reads the route table from `app.openapi()` rather than walking
    `app.routes` directly: FastAPI's internal representation of an included
    router's routes is not part of its stable public API and has changed
    shape between versions, while the OpenAPI schema -- the same one that
    drives the real `/docs` page -- is guaranteed to reflect everything
    actually registered.
    """
    # flush=True: stdout is fully (not line-) buffered whenever it isn't an
    # interactive terminal -- piped through `tee`, redirected to a log file,
    # captured by a process supervisor -- so without it this can sit in the
    # buffer indefinitely instead of showing up when it matters most.
    print("\nAvailable endpoints:", flush=True)
    paths = app.openapi()["paths"]
    for path in sorted(paths):
        methods = sorted(m.upper() for m in paths[path] if m.upper() != "OPTIONS")
        for method in methods:
            print(f"  {method:<7} {path}", flush=True)
    print("  interactive docs: /docs\n", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    _print_routes(app)
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
