# Three Scenarios

Greenfield, brownfield, and ambiguous — run live through the actual `asdlc`
CLI, not simulated. Every transcript below is real output from this
repository's code (deterministic provider, no network). Run ids, timings and
generated content will differ slightly between runs — the deterministic
provider makes the *source code* reproducible for a fixed input, not the
wall-clock timings or generated ids.

Each scenario shows: requirement understanding, task decomposition,
orchestration (parallel dispatch, gates, governance), and validation.

## Scenario 1: Greenfield

**Ask:** build the URL shortener from nothing.

```
$ asdlc build "Build a URL shortener with core APIs, custom aliases, \
    expiration, click analytics, and rate limiting for reliability." \
    --title "URL Shortener"

provider: deterministic  autonomy: bounded
run id: cli-greenfield_c46fd7988015
              Stage Results
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┓
┃ stage          ┃ status    ┃ duration ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━┩
│ requirements   │ succeeded │ 0.001s   │
│ architecture   │ succeeded │ 0.002s   │
│ planning       │ succeeded │ 0.002s   │
│ implementation │ succeeded │ 0.005s   │
│ docs           │ succeeded │ 0.010s   │
│ security       │ succeeded │ 0.010s   │
│ testing        │ succeeded │ 0.408s   │
│ validation     │ succeeded │ 0.002s   │
│ release        │ succeeded │ 0.002s   │
└────────────────┴───────────┴──────────┘
                    Findings
┏━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ severity ┃ category   ┃ summary                          ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ medium   │ compliance │ [CMP002] 8 code artifact(s) ...  │
│ medium   │ compliance │ [CMP002] 8 code artifact(s) ...  │
│ medium   │ security   │ no authentication on any endpoint│
│ low      │ security   │ long_url scheme/host unrestricted│
└──────────┴────────────┴───────────────────────────────────┘
reliability: success_rate=100% retries=0 rollbacks=0 mttr=n/a e2e=0.42s
materialized to target/ -- see target/README.md to run it
```

**Requirement understanding.** `docs/requirements.md` in the output shows
the normalized problem statement, 8 functional requirements, and every
aspect the raw statement left unaddressed (down to a handful, since this
statement names aliases/expiry/analytics/rate-limiting explicitly) recorded
as a non-blocking assumption with a confidence score — not silently decided.

**Task decomposition.** `docs/plan.md` shows 9 dependency-ordered tasks:
storage and codec first (everything else depends on them), then create and
redirect, then the four optional-capability tasks — each one present
*because* `requirements` put the matching capability in scope.

**Orchestration.** `architecture`/`planning` fan out in parallel off
`requirements`; `testing`/`security`/`docs` fan out in parallel off
`implementation` — `testing` takes ~0.3–0.4s (it runs a real pytest
subprocess against the generated code) while `security`/`docs` finish in
single-digit milliseconds, and the frontier scheduler does not make the fast
branches wait for the slow one. `validation` is the synchronization barrier:
it does not start until all three are satisfied. The audit ledger for this
run holds 88 events with an intact hash chain — 21 artifacts written, 12
decisions recorded with rationale, 9 policy evaluations, one approval
checkpoint (`release`, the graph's mandatory `high_impact` node) granted.

**Validation.** `docs/risk_register.md` synthesizes every ambiguity
assumption plus the security findings above into one document; `docs/
engineering_summary.md` is the final plan/rationale/artifacts/risks/
assumptions/limitations write-up, and carries the release recommendation.

The generated service is genuinely runnable — see the root
[README.md](../README.md) for a live curl transcript against exactly this
output.

## Scenario 2: Brownfield

**Setup.** A brownfield run needs an existing codebase to reason about, so
first build a deliberately minimal "legacy" service — just the core create
and redirect endpoints, nothing else:

```
$ asdlc build "Build a minimal URL shortener with just the core create \
    and redirect APIs." --title "Legacy URL Shortener" --out legacy_seed
```

This produces 7 source files — no `app/middleware.py`, no alias/expiry/stats
handling, exactly what was asked for and nothing more (a capability the
requirement doesn't name is left out of scope with a recorded assumption,
not silently built — see [ARCHITECTURE.md §6](ARCHITECTURE.md#6-key-design-decisions)).

**Ask:** seed the workspace with that legacy codebase, and request a genuine
enhancement:

```
$ asdlc build "Add expiration support to existing shortened links." \
    --kind brownfield --seed legacy_seed --title "Add Link Expiration"

provider: deterministic  autonomy: bounded
run id: cli-brownfield_2cbf73aa3944
              Stage Results
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┓
│ requirements   │ succeeded │ 0.002s   │
│ architecture   │ succeeded │ 0.004s   │
│ planning       │ succeeded │ 0.004s   │
│ implementation │ succeeded │ 0.004s   │
│ ...            │ succeeded │ ...      │
└────────────────┴───────────┴──────────┘
reliability: success_rate=100% ... e2e=0.34s
```

**Codebase reasoning** (§4.3 of the assessment — architecture's brownfield
path). `docs/architecture.md` in the output:

```
## Brownfield impact analysis
Matched against requirement keywords ['existing', 'expiration', 'links',
'shortened', 'support']:
- `app/storage.py` (matched: shortened)
```

This is a real scan of the seeded workspace's actual source files (docs and
reports excluded — see [ARCHITECTURE.md §6](ARCHITECTURE.md#6-key-design-decisions)
for why), not an assertion. It is also an honest one: lexical overlap can
only find files that already relate to the topic by vocabulary, and the
legacy seed's `app/routes.py` has no trace of "expiration" yet — a real
static-analysis pass (import/call graphs) would do better here; this is a
documented limitation, not hidden.

**Task decomposition stays narrow.** `docs/plan.md`:

```
- data model and storage repository
- short-code generation
- create endpoint
- redirect endpoint
- expiration handling
- auth scaffolding note
```

Only `expiration handling` was added beyond the unconditional core tasks —
custom alias, analytics and rate-limiting are correctly absent, because
nothing in this requirement asked for them.

**The actual diff**, `app/routes.py`, legacy seed vs. brownfield output:

```diff
-    row_id = storage.insert("", str(payload.long_url), now)
+    row_id = storage.insert("", str(payload.long_url), now,
+                             expires_at=payload.expires_at.isoformat()
+                             if payload.expires_at else None)
+
+    if row["expires_at"] and row["expires_at"] < _utcnow_iso():
+        raise HTTPException(status_code=410, detail="this link has expired")
```

A narrow, incremental, reviewable change — not a regeneration of the whole
service. `docs/plan.json` + the decision ledger tie each line back to the
task and the decision that produced it.

**Validation.** The security review now correctly flags "the create endpoint
has no rate limiting" (silent in the fully-featured greenfield build,
because that build has rate limiting enabled) — the same guardrail rules,
producing findings specific to what this particular build actually
contains.

## Scenario 3: Ambiguous

**Ask**, deliberately underspecified:

```
$ asdlc build "Make it better, TBD on details" --kind ambiguous \
    --title "Improve The Shortener"

provider: deterministic  autonomy: bounded
run id: cli-ambiguous_483cdcefdd0c
              Stage Results
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┓
│ requirements   │ succeeded │ 0.002s   │
│ architecture   │ blocked   │ -        │
│ planning       │ blocked   │ -        │
│ implementation │ blocked   │ -        │
│ docs           │ skipped   │ -        │
│ security       │ blocked   │ -        │
│ testing        │ blocked   │ -        │
│ validation     │ blocked   │ -        │
│ release        │ blocked   │ -        │
└────────────────┴───────────┴──────────┘
                              Findings
┏━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ high     │ requirements │ blocking ambiguity: the request does not  ┃
┃          │              │ name a specific capability to add, change ┃
┃          │              │ or fix (...). Which concrete capability   ┃
┃          │              │ should this work target?                 ┃
┗━━━━━━━━━━┻━━━━━━━━━━━━━━┻━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
reliability: success_rate=12% retries=0 rollbacks=0 mttr=n/a e2e=0.00s
run ended in halted (blocking_failure); nothing promoted -- run id:
cli-ambiguous_483cdcefdd0c
```

`requirements` succeeds — it correctly recognizes the statement, decides the
gap is consequential enough that no default assumption is safe, and records
a **blocking** ambiguity rather than a silent one. `NoBlockingAmbiguityGate`
then holds `architecture` and `planning` at entry — the exit code is 1, and
nothing is promoted to `target/`. This is controlled autonomy refusing to
guess at scope, exactly as intended: an unattended agent building the wrong
thing is worse than an agent that stops and asks.

**Inspecting the halt**, in a separate process invocation — proving state
genuinely persisted, not just held in memory:

```
$ asdlc status cli-ambiguous_483cdcefdd0c
[same stage table and finding as above, reloaded from state.json + the ledger]
```

**Resolving it.** A human reads the blocking question and answers with a
concrete, restated requirement (a *replacement*, not an append — see
[ARCHITECTURE.md §3.6](ARCHITECTURE.md#36-dynamic-re-planning-two-distinct-primitives)
for why appending to the original vague text doesn't work: the leftover
"TBD" would keep tripping the same check):

```
$ asdlc clarify cli-ambiguous_483cdcefdd0c \
    "Add a bulk-delete endpoint that revokes multiple short codes at once, \
     plus click analytics via a stats endpoint."

              Stage Results
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┓
│ requirements   │ succeeded │ 0.002s   │
│ architecture   │ succeeded │ 0.002s   │
│ planning       │ succeeded │ 0.002s   │
│ implementation │ succeeded │ 0.003s   │
│ docs           │ succeeded │ 0.009s   │
│ security       │ succeeded │ 0.010s   │
│ testing        │ succeeded │ 0.309s   │
│ validation     │ succeeded │ 0.002s   │
│ release        │ succeeded │ 0.002s   │
└────────────────┴───────────┴──────────┘
reliability: success_rate=100% ... e2e=6.18s
materialized to target/ -- see target/README.md to run it
```

`Engine.retry_stage()` reset `requirements` plus everything the blocking
ambiguity had made unreachable — including `docs`, which had gone `SKIPPED`
(not `BLOCKED`) and needed a second, distinct fix in the retry-reset logic
to come back at all (see [ARCHITECTURE.md §3.6](ARCHITECTURE.md#36-dynamic-re-planning-two-distinct-primitives)).
`requirements` re-ran against the clarified statement, produced an
unambiguous normalized requirement, and every previously-blocked stage got
its chance and succeeded. The blocking-ambiguity finding from the first
attempt remains visible in the run's finding history — the audit trail
does not erase the fact that clarification was needed, only that it was
resolved.

## What the three scenarios together demonstrate

- **Requirement understanding** that actually varies with the input: a
  precise statement produces almost no ambiguities, a sparse one produces
  several non-blocking assumptions, a vague one produces a blocking halt.
- **Task decomposition** that tracks scope precisely — greenfield gets 9
  tasks, the narrow brownfield enhancement gets exactly one beyond the core,
  and nothing is built that wasn't asked for.
- **Orchestration**: real parallel dispatch with a synchronization barrier,
  entry/exit gates, mandatory approval on release regardless of autonomy,
  and two distinct, independently-tested primitives for getting a stalled
  run moving again.
- **Validation**: risk registers and security reviews that reflect what was
  *actually built* in that specific run, not a generic template — the
  missing-rate-limiting finding appears only when rate limiting was, in
  fact, left out.
