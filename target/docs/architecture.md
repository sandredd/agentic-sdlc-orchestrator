# Architecture

## Key decisions

### how should short codes be generated?
**Choice:** random base62 token, 6 characters, bounded collision-retry on both the generated case and a caller-supplied custom alias
**Rationale:** an id-derived encoding was considered and rejected: it makes every code enumerable -- given one code, incrementing it walks every link the service has ever created, with no auth in front of any of it. A random token costs one extra existence check per create (collisions are astronomically rare at 62**6 codes) in exchange for codes that reveal nothing about each other
**Alternatives considered:** base62-encoded auto-increment row id (rejected: enumerable), hash of the long URL (not unique)

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
