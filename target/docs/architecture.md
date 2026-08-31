# Architecture

## Key decisions

### how should short codes be generated?
**Choice:** base62-encoded auto-increment row id, 6+ characters, collision-checked on custom alias only
**Rationale:** monotonic ids avoid a random-collision retry loop on the hot create path; base62 keeps codes short and URL-safe
**Alternatives considered:** random token + collision retry, hash of the long URL (not unique)

### how is persistence structured?
**Choice:** a single SQLite table behind a repository interface (`Storage` protocol)
**Rationale:** the requirement did not mandate a specific database; SQLite needs no external service for a prototype, and the repository interface is what makes swapping to Postgres later a config change, not a rewrite
**Alternatives considered:** in-memory dict (no durability), Postgres (operational overhead unjustified at this stage)

### how is create-endpoint abuse mitigated?
**Choice:** in-memory fixed-window rate limiter, per client IP, applied as ASGI middleware
**Rationale:** meets the stated reliability goal without adding an external dependency (Redis); documented as not distributed-safe -- multiple app instances would each keep their own counters
**Alternatives considered:** Redis-backed limiter (correct under scale-out, adds an operational dependency this prototype doesn't need yet)

## Layering
- `app/routes.py` -- HTTP layer (FastAPI routers, request/response schemas)
- `app/storage.py` -- persistence (SQLite repository behind a narrow interface)
- `app/codec.py` -- short-code generation (base62 encode/decode)
- `app/middleware.py` -- cross-cutting reliability concerns (rate limiting)
- `app/config.py` -- environment-driven settings

See `api/openapi.yaml` for the full API contract.
