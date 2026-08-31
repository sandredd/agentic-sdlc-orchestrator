# Testing Approach, Trade-offs, and Limitations

## Testing approach

**250 tests**, organized by what they actually exercise rather than by
module:

| Layer | Files | What's covered |
|---|---|---|
| Domain contracts | `tests/orchestrator/test_contracts.py` | Frozen models, content-addressed artifact hashing, severity/risk ordering |
| Audit ledger | `test_ledger.py` | Hash-chain integrity, tamper detection (an edited event body invalidates every successor), concurrent-append linearization |
| Sandboxed workspace | `test_workspace.py` | Path traversal / symlink-escape rejection, snapshot/restore removes orphaned files, not just reverts modified ones |
| DAG | `test_graph.py` | Cycle detection (named in the error), static data-flow validation, `ALL` vs. `ANY` join semantics, optional-bypass vs. hard-block classification |
| Engine | `test_engine.py`, `test_governance.py` | Frontier scheduling (parallel branches genuinely overlap, a fast branch isn't held by a slow sibling), per-stage transactionality, three-tier failure severity, safe-stop drain, retry/fallback, policy enforcement, approval checkpoints including cross-process resume, cascading rollback, both re-planning primitives |
| Config / policy / resilience / approvals / metrics / replanning | `test_config.py`, `test_policy.py`, `test_resilience.py`, `test_approvals.py`, `test_metrics.py`, `test_replanning.py` | Each governance concern unit-tested in isolation before the integration tests exercise it composed with the others |
| Agents | `tests/agents/*.py` | Each of the nine stage agents individually, plus `test_pipeline_end_to_end.py` running all nine through the real `Engine` |
| CLI | `tests/test_cli.py` | Every command (`build`/`status`/`resume`/`clarify`) through Typer's `CliRunner` against real runs — not mocked |

Run them:

```bash
pip install -e ".[dev]"
pytest -q
```

### Why the testing agent actually executes tests

`orchestrator/agents/testing.py` does not just generate a pytest file — it
materializes the accumulated workspace into a scratch directory and runs
`pytest` in a real subprocess against it, then reports the genuine pass/fail
count as a `StageResult` metric and, on a real failure, a `HIGH`-severity
finding. A generated test nobody ran is a to-do, not validation; the
assessment's "Output Generation/Validation" objective is not satisfied by
plausible-looking test code that has never executed. This was verified
directly during development by deliberately breaking a generated endpoint
(flipping the redirect status code from 302 to 200) and confirming the
testing stage caught it — a real regression, not a scripted assertion.

### Why agent tests use the deterministic provider

Every agent test runs against `DeterministicProvider` — no network, fully
reproducible, safe in CI. `RequirementsAgent` additionally has a
model-backed code path (`Agent.think()`), tested separately with a fake
provider that returns valid JSON, so the parsing/fallback logic is verified
without a live API dependency. See [README.md](../README.md#using-a-real-model)
for enabling the real Claude provider.

### Bugs the tests actually found

Several defects listed here were caught by tests failing unexpectedly during
development, not designed in from the start — worth calling out because it's
evidence the test suite does real work rather than encoding assumptions that
happen to match the implementation:

- **Ledger tamper detection was backwards.** The first `verify()` chained on
  each event's *stored* hash rather than a freshly recomputed one, so a
  forged event body didn't invalidate its successors — one forged event in
  isolation would have verified clean. Caught immediately by
  `test_tampering_is_detected`.
- **`retry_stage()` didn't reset `SKIPPED` stages**, only `BLOCKED` ones —
  an optional stage bypassed because its dependency was unreachable stayed
  skipped forever even after that dependency was fixed. Caught by actually
  running the ambiguous scenario end to end through the CLI, not by a
  pre-written unit test — the fix then got a regression test.
- **The requirements heuristic silently built every optional capability**
  regardless of whether the requirement asked for it — only the recorded
  *assumption* text differed by aspect. Caught the same way: running a
  deliberately minimal brownfield seed build and finding `app/middleware.py`
  in the output despite never having requested rate limiting.
- **A keyword collision**: the always-on "persist shortened URLs and their
  analytics durably" line accidentally satisfied planning's gate for the
  *analytics capability itself*, so every build silently included a stats
  endpoint. Found while writing this documentation, from a real brownfield
  transcript whose plan unexpectedly included `stats endpoint` — reworded
  the sentence, added a regression test asserting the word "analytic" does
  not leak into the functional list for a requirement that never mentions
  it.
- **`--no-auto-approve` without `--interactive` was a silent no-op** — it
  left `Engine`'s own default provider (`AutoApproveProvider`) in place
  instead of producing a genuinely pending checkpoint.
- **Dependency drift broke `target/`'s own test instructions**, reported by
  a real run against a fresh environment: a newer `starlette` release made
  its `TestClient` prefer an `httpx2` package, falling back to the (still
  supported, deprecation-warned) plain `httpx` only if `httpx2` was absent.
  `target/README.md`'s `pip install pytest httpx` predated that change and
  would break once the fallback is eventually removed. Separately, `target/`
  had no pytest config of its own, so running `pytest` from inside a
  standalone copy of it silently walked up and applied *this* repository's
  `asyncio_mode` setting — harmless today (the generated tests are all
  synchronous) but wrong, and the source of a confusing "Unknown config
  option" warning in an environment without `pytest-asyncio` installed.
  Fixed by depending on `httpx2` directly (both in this project's own dev
  extras and in the generated README's test instructions) and by having
  `implementation` emit a minimal `pyproject.toml` that scopes `target/` to
  its own pytest rootdir — verified by reproducing the original failure in
  a throwaway venv and confirming it no longer occurs.

In each case, the fix shipped with the test that would have caught it —
`git log` on this repository shows the commit message calling out the bug,
the root cause, and the regression test added, per phase.

## Trade-offs

- **Deterministic-first over LLM-first.** The system is designed to run
  fully and reproducibly with zero API calls. This trades away the
  requirements agent's ability to genuinely understand novel phrasing in
  deterministic mode (it's a keyword heuristic, not NLU) for a system that
  is safe to grade, safe in CI, and free to run. A live model is one
  environment variable away (`ANTHROPIC_API_KEY`).
- **Lexical brownfield impact analysis over static analysis.** Scanning file
  content for keyword overlap is orders of magnitude simpler than building
  an import/call graph, and is honest about its limit (see below). A real
  static-analysis pass would find files the current code has no way to
  find — this was accepted as the right scope for a prototype rather than
  building a Python AST/import analyzer.
- **In-process rate limiting and no authentication in the generated
  service.** Both are explicit, documented prototype-scope decisions (see
  the generated `docs/risk_register.md` in any run's output), not oversights
  — the alternative (Redis-backed limiting, API-key auth) adds operational
  dependencies a prototype doesn't need yet.
- **Approval checkpoints enforced at stage *exit* only.** `ApprovalPolicy`
  models entry-point checkpoints (relevant to `AutonomyLevel.SUGGEST`, where
  even attempting a high-impact action should require sign-off first) and is
  independently unit-tested, but the engine does not yet block *dispatch* on
  them — see Limitations below.

## Limitations

- **The deterministic requirements heuristic cannot detect negation.**
  "no expiration" still lexically matches the "expir" keyword the same way
  "add expiration" does, so a requirement phrased as an explicit exclusion
  can be mis-read as a request. Demonstrated directly in development (see
  [SCENARIOS.md](SCENARIOS.md#scenario-2-brownfield) for the workaround used
  when writing the brownfield demo). A real NLU pass (or the live-model
  code path) handles this correctly; documented rather than chased with
  fragile negation regexes in the fallback path.
- **Brownfield codebase reasoning is lexical overlap, not static analysis.**
  It can only point at files that already relate to the topic by
  vocabulary — a capability with zero existing trace in the code (the
  common case for "add X") won't be found by keyword matching alone, only
  by structural signals a real static-analysis pass would need to compute.
- **Entry-point approval checkpoints are modeled but not enforced.**
  `ApprovalPolicy.evaluate(..., ApprovalPoint.ENTRY)` is implemented and
  tested (`test_approvals.py`), but `Engine._dispatch` does not currently
  consult it before starting a stage — only the exit-point checkpoint is
  wired into the dispatch loop. `AutonomyLevel.SUGGEST`'s "propose, don't
  execute" semantics are therefore not yet enforced at the point of
  execution.
- **Reliability-metric findings can vary slightly between runs of the same
  input.** True parallel dispatch means `security` and `testing` race to
  read `state.artifacts`; depending on which finishes first, `security`'s
  batched policy re-scan may or may not see `testing`'s test-file artifact
  yet, changing whether the `TestsAccompanyCodeRule` finding fires once or
  twice for the same underlying gap. The generated *code* is unaffected —
  only which stage happens to report a given (correct) finding first.
- **No distributed execution.** The engine, ledger, and workspace are all
  single-process. A production system coordinating many concurrent runs
  would need the ledger and state store to be a real database, not a JSONL
  file and a JSON snapshot — the abstractions (`Ledger`, `RunState`) are
  written so that swap is a storage-layer change, not a redesign, but it
  has not been built.
- **The Anthropic provider is untested against a live API** in this
  submission (no network access during development). `AnthropicProvider`
  is exercised by its own construction/error-path unit tests and by the
  same `Agent.think()` contract every other path uses, but has not been
  run end-to-end against `api.anthropic.com`.
