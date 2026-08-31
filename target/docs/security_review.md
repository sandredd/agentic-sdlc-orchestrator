# Security Review

**Capabilities reviewed:** {'alias': True, 'expiry': True, 'stats': True, 'rate_limit': True}

## Findings
- **[medium] [CMP002] 8 code artifact(s) produced with no accompanying tests**
  - Untested generated code transfers the verification burden to the reviewer.
  - remediation: add unit tests, or let the downstream test stage cover them
- **[medium] the service has no authentication on any endpoint**
  - acceptable for this prototype's stated scope, but every write and delete endpoint is currently open to anyone who can reach the service
  - remediation: add API-key or OAuth2 auth in front of POST/DELETE before any deployment beyond a local prototype
- **[low] long_url is validated as a well-formed URL but not restricted by scheme or host**
  - a client can shorten a javascript:, file:, or internal-network URL; the redirect will happily forward a victim there
  - remediation: restrict accepted schemes to http/https and consider an allow/deny list for internal address ranges (SSRF-style open-redirect risk)
