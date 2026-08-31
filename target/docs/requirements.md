# Requirements

**Problem statement.** Build a URL shortener with core APIs, custom aliases, expiration, click analytics, and rate limiting for reliability. -- normalized for a URL shortener service (greenfield scenario)

**Risk:** medium

## In scope
- accept a long URL and return a shortened code
- redirect a shortened code to its original long URL
- accept an optional custom alias for a shortened URL
- support an optional expiration time on a shortened URL
- expose all endpoints without authentication (prototype scope)
- record and expose click analytics per shortened URL
- persist shortened URLs and their analytics durably
- rate-limit URL creation to protect the service from abuse

## Out of scope
- multi-region deployment
- a user-facing web dashboard beyond the API
- billing or quota enforcement

## Functional requirements
- accept a long URL and return a shortened code
- redirect a shortened code to its original long URL
- accept an optional custom alias for a shortened URL
- support an optional expiration time on a shortened URL
- expose all endpoints without authentication (prototype scope)
- record and expose click analytics per shortened URL
- persist shortened URLs and their analytics durably
- rate-limit URL creation to protect the service from abuse

## Non-functional requirements
- p99 redirect latency under 100ms on the prototype's local SQLite backend
- the create endpoint is rate-limited to reduce abuse
- no request is silently dropped: failures return a clear HTTP error

## Acceptance criteria
- POST a long URL and receive a working short code _(verified by integration)_
- GET the short code redirects (302) to the original long URL _(verified by integration)_
- the stats endpoint reflects an incremented click_count after a redirect _(verified by integration)_

## Ambiguities and assumptions
- **[assumed (confidence 55%)]** the requirement does not specify a detail of: expose all endpoints without authentication (prototype scope)?
  - why it matters: downstream design and implementation need a concrete default
  - assumption: no authentication in this prototype; every endpoint is public
- **[assumed (confidence 80%)]** the requirement does not specify a detail of: persist shortened URLs and their analytics durably?
  - why it matters: downstream design and implementation need a concrete default
  - assumption: SQLite backs the prototype behind a repository interface swappable for production scale
