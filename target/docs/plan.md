# Task Decomposition

**Sequencing rationale.** core CRUD/redirect tasks are unconditional; optional tasks (custom alias, expiration, analytics, rate limiting) are included only when the normalized functional requirements call for them, so a narrow brownfield change gets a narrow plan rather than the full greenfield build-out. Storage and code generation are sequenced first because every endpoint depends on them.

## Tasks
- **data model and storage repository** (2pt, risk=low)
  - SQLite schema + repository interface for URLs
  - depends on: none
- **short-code generation** (1pt, risk=low)
  - base62 encode/decode with a uniqueness guarantee
  - depends on: data model and storage repository
- **create endpoint** (2pt, risk=low)
  - POST /api/urls: validate, generate/accept code, persist
  - depends on: data model and storage repository, short-code generation
- **redirect endpoint** (2pt, risk=low)
  - GET /{code}: lookup, increment analytics, 302 or 404/410
  - depends on: data model and storage repository
- **custom alias handling** (1pt, risk=medium)
  - accept custom_alias, enforce uniqueness, 409 on collision
  - depends on: data model and storage repository, create endpoint
- **expiration handling** (1pt, risk=medium)
  - accept/validate expires_at; 410 once passed
  - depends on: data model and storage repository, redirect endpoint
- **stats endpoint** (1pt, risk=low)
  - GET /api/urls/{code}/stats: click_count, last_accessed_at
  - depends on: data model and storage repository, redirect endpoint
- **rate limiting middleware** (2pt, risk=medium)
  - fixed-window limiter on the create endpoint
  - depends on: create endpoint
- **auth scaffolding note** (1pt, risk=medium)
  - document the no-auth prototype boundary explicitly
  - depends on: none
