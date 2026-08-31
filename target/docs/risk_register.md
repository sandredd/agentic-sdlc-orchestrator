# Risk Register

**Test status:** 8 passed, 0 failed (ran=True)
**Security findings:** 3
**Overall requirement risk:** medium

## Risks, trade-offs and mitigations
- **[medium] compliance: [CMP002] 8 code artifact(s) produced with no accompanying tests**
  - trade-off: 2 finding(s) in this category, worst=medium
  - mitigation: add unit tests, or let the downstream test stage cover them
- **[medium] security: the service has no authentication on any endpoint**
  - trade-off: 2 finding(s) in this category, worst=medium
  - mitigation: add API-key or OAuth2 auth in front of POST/DELETE before any deployment beyond a local prototype
- **[medium] SQLite is a single-file, single-writer database**
  - trade-off: chosen for zero operational overhead in a prototype
  - mitigation: the repository interface in app/storage.py is the seam a Postgres migration would go through without touching route handlers
- **[medium] the rate limiter's state is in-process and per-instance**
  - trade-off: avoids an external dependency (Redis) at prototype scale
  - mitigation: not distributed-safe; replace with a shared store before running more than one instance
- **[low] the requirement does not specify a detail of: expose all endpoints without authentication (prototype scope)?**
  - trade-off: no authentication in this prototype; every endpoint is public
  - mitigation: documented assumption; revisit if usage patterns contradict it
- **[low] the requirement does not specify a detail of: persist shortened URL records durably?**
  - trade-off: SQLite backs the prototype behind a repository interface swappable for production scale
  - mitigation: documented assumption; revisit if usage patterns contradict it

## Failure scenarios considered
- a request for an unknown code returns 404 rather than a 500 or a silent redirect to a default page
- a duplicate custom alias is rejected with 409 rather than silently overwriting the existing mapping
- an expired link returns 410 rather than continuing to redirect indefinitely
- a malformed long_url is rejected with 422 at the boundary rather than stored and failing later at redirect time
- create-endpoint abuse is bounded by the rate limiter rather than unbounded
