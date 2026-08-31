# agentic-sdlc-orchestrator

A working prototype that transforms a requirement into a reviewable
engineering outcome using an agentic execution model: requirement
understanding, task decomposition, multi-step execution, and output
generation/validation, under an orchestration layer with explicit
governance rather than a simple linear task chain.

Two things live here:

1. **The orchestrator** (`orchestrator/`) — an execution engine that drives a
   requirement through nine SDLC stages via an explicit dependency graph,
   with entry/exit gates, parallel dispatch with synchronization, universal
   policy guardrails, human approval checkpoints, bounded retry/fallback/
   rollback, an audit-grade hash-chained ledger, reliability metrics, and
   dynamic re-planning.
2. **The target system** (`target/`) — a URL shortener service, *generated
   by* the orchestrator, not hand-written. `asdlc build` reproduces it (or a
   variant) from a requirement statement.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it's built,
[docs/SCENARIOS.md](docs/SCENARIOS.md) for the three required scenarios
(greenfield / brownfield / ambiguous) with real transcripts,
[docs/TESTING.md](docs/TESTING.md) for testing approach, trade-offs and
limitations, and [docs/ENGINEERING_SUMMARY.md](docs/ENGINEERING_SUMMARY.md)
for the final engineering summary.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

That installs the orchestrator, its CLI (`asdlc`), and everything needed to
run it, run the generated service's own tests, and run this repo's own test
suite.

## Quick start

Run the orchestrator on a fresh requirement — this regenerates `target/`:

```bash
asdlc build "Build a URL shortener with core APIs, custom aliases, expiration, click analytics, and rate limiting for reliability."
```

```
provider: deterministic  autonomy: bounded
run id: cli-greenfield_...
              Stage Results
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┓
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
reliability: success_rate=100% retries=0 rollbacks=0 mttr=n/a e2e=0.42s
materialized to target/ -- see target/README.md to run it
```

No network access and no API key are required — the default provider is
fully deterministic and reproducible. See
[Using a real model](#using-a-real-model) below to enable Claude.

Run the three required scenarios (greenfield / brownfield / ambiguous),
with real, current transcripts, in
[docs/SCENARIOS.md](docs/SCENARIOS.md). Short version:

```bash
# greenfield
asdlc build "Build a URL shortener with core APIs, custom aliases, expiration, click analytics, and rate limiting for reliability."

# brownfield: seed with an existing codebase, request an enhancement
asdlc build "Build a minimal URL shortener with just the core create and redirect APIs." --out legacy_seed
asdlc build "Add expiration support to existing shortened links." --kind brownfield --seed legacy_seed

# ambiguous: halts on a blocking ambiguity, then resolve it
asdlc build "Make it better, TBD on details" --kind ambiguous
asdlc status <run-id>          # inspect what's blocking, from a separate process
asdlc clarify <run-id> "Add a bulk-delete endpoint that revokes multiple short codes at once, plus click analytics via a stats endpoint."
```

## Run the generated service

`target/` is committed, generated output — a real, standalone FastAPI
service, runnable independent of the orchestrator that produced it:

```bash
cd target
pip install -r requirements.txt
uvicorn app.main:app --reload
```

```
$ curl -s http://localhost:8000/health
{"status":"ok"}

$ curl -s -X POST http://localhost:8000/api/urls \
    -d '{"long_url": "https://www.anthropic.com/claude-code", "custom_alias": "claude-code"}'
{"code":"claude-code","short_url":"http://localhost:8000/claude-code",
 "long_url":"https://www.anthropic.com/claude-code",
 "created_at":"2026-08-31T04:49:58.734405Z","expires_at":null}

$ curl -si http://localhost:8000/claude-code | head -1
HTTP/1.1 302 Found

$ curl -s http://localhost:8000/api/urls/claude-code/stats
{"code":"claude-code","click_count":1,"last_accessed_at":"2026-08-31T04:49:58.747251Z"}
```

Run its own test suite:

```bash
cd target
pytest -q
```

See `target/README.md` for the full API reference and `target/docs/` for
the requirements, architecture, plan, security review and risk register
that specific run produced.

## Run the orchestrator's own test suite

```bash
pytest -q          # 250 tests
ruff check .        # lint
```

See [docs/TESTING.md](docs/TESTING.md) for what's covered, why the testing
agent actually executes generated tests rather than just emitting them, and
a list of real bugs the test suite caught during development.

## CLI reference

| Command | Does |
|---|---|
| `asdlc build STATEMENT [options]` | Run the full nine-stage pipeline; materialize the result into `--out` (default `target/`) on success |
| `asdlc status RUN_ID` | Show a persisted run's stage table, findings, and any pending approvals |
| `asdlc resume RUN_ID [--approve\|--reject] [--note ...]` | Answer a pending human approval checkpoint and continue |
| `asdlc clarify RUN_ID "restated requirement"` | Replace a blocked run's requirement and retry from `requirements` |

Key `build` options: `--kind greenfield|brownfield|ambiguous`, `--seed DIR`
(existing codebase for brownfield), `--autonomy suggest|supervised|bounded|
autonomous`, `--interactive` (prompt on the terminal for each checkpoint),
`--no-auto-approve` (prove a checkpoint genuinely holds).

## Using a real model

By default `asdlc` uses `DeterministicProvider` — no network, fully
reproducible. To use Claude for the requirements stage's natural-language
understanding:

```bash
pip install -e ".[llm]"
export ANTHROPIC_API_KEY=sk-ant-...
asdlc build "..."
```

Every other agent's core logic (code generation, test execution, policy
scanning) stays deterministic either way — implementation is exactly the
stage where a reviewer least wants the same input to produce a different
diff each run. See [docs/ARCHITECTURE.md §6](docs/ARCHITECTURE.md#6-key-design-decisions).

## Project layout

```
orchestrator/    the orchestration engine, governance layer, and nine SDLC agents
target/          generated deliverable: the URL shortener service
tests/           250 tests for the orchestrator itself
docs/            architecture, scenarios, testing/limitations, engineering summary
assessment.pdf   the original assignment
```
