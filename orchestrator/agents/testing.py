"""Stage 5: Test Generation & Execution.

Generates a pytest suite against the implementation stage's capability flags
(so tests exist only for endpoints that were actually generated), writes it
into the workspace alongside the source, and then -- this is the part that
makes it validation rather than just generation -- actually runs it with a
subprocess `pytest` against the materialized workspace and reports the real
result. A test file nobody ran is a to-do, not a validated outcome; §6 of the
assessment ("Validation and Risk Control") is not satisfied by generating
plausible-looking test code that has never been executed.

A failing run is not treated as a stage failure by default: it raises a
HIGH-severity finding (or BLOCKER if nothing passed at all) and lets the
exit gate / policy layer decide whether that blocks the pipeline, since a
red test suite is exactly the kind of signal a human approval checkpoint
downstream should see verbatim rather than have silently swallowed.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.agents.base import Agent
from orchestrator.contracts import ArtifactKind, Severity

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState


class TestingAgent(Agent):
    __test__ = False  # not a pytest test class; the name just collides with pytest's heuristic
    stage_name = "testing"

    async def run(self, node: StageNode, state: RunState) -> object:
        raw_code = state.context.get("code", reader=self.stage_name)
        caps = raw_code["capabilities"] if raw_code else {}

        test_source = _test_api_py(caps)
        artifacts = [
            self.artifact("tests/__init__.py", ArtifactKind.TEST, ""),
            self.artifact("tests/test_api.py", ArtifactKind.TEST, test_source),
        ]

        outcome = await self._execute(state)

        findings = []
        if outcome.ran:
            severity = None
            if outcome.failed and outcome.passed == 0:
                severity = Severity.BLOCKER
            elif outcome.failed:
                severity = Severity.HIGH
            if severity is not None:
                findings.append(
                    self.finding(
                        severity,
                        "testing",
                        f"{outcome.failed} of {outcome.passed + outcome.failed} test(s) failed",
                        detail=outcome.detail[-4000:],
                        remediation="fix the failing behavior or the test; do not delete the test",
                    )
                )
        else:
            findings.append(
                self.finding(
                    Severity.MEDIUM,
                    "testing",
                    "the generated suite could not be executed in this environment",
                    detail=outcome.detail[-4000:],
                    remediation="install the [dev] extra (pytest, httpx, fastapi) and re-run",
                )
            )

        report = self._render_report(outcome)
        artifacts.append(self.artifact("docs/test_report.md", ArtifactKind.REPORT, report))

        decision = self.decision(
            "was the generated suite actually executed against the generated code?",
            f"yes: {outcome.passed} passed, {outcome.failed} failed" if outcome.ran else "no",
            "a generated test file is not evidence of correctness until it has actually run "
            "against the code it targets; this stage executes pytest in a real subprocess "
            "rather than only emitting the file",
            confidence=0.95,
        )

        summary = (
            f"generated and ran the test suite: {outcome.passed} passed, {outcome.failed} failed"
            if outcome.ran
            else "generated the test suite; execution was not possible in this environment"
        )
        return self.result(
            summary=summary,
            artifacts=tuple(artifacts),
            findings=tuple(findings),
            decisions=(decision,),
            context={
                "test_report": {
                    "ran": outcome.ran,
                    "passed": outcome.passed,
                    "failed": outcome.failed,
                }
            },
            metrics={"tests_passed": float(outcome.passed), "tests_failed": float(outcome.failed)},
        )

    async def _execute(self, state: RunState) -> _TestOutcome:
        """Materialize every artifact accumulated so far into a scratch
        directory and run pytest there. A scratch copy, not the run's own
        workspace, so a flaky or destructive test can never corrupt the audit
        trail's artifact snapshots."""
        if self.workspace is None:
            return _TestOutcome(
                ran=False, passed=0, failed=0, detail="no workspace bound to this agent"
            )

        with tempfile.TemporaryDirectory(prefix="asdlc-test-") as tmp:
            root = Path(tmp)
            for path in self.workspace.files():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(self.workspace.read(path), encoding="utf-8")
            for artifact_path, content in self._pending_artifacts(state):
                target = root / artifact_path
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_text(content, encoding="utf-8")

            if not (root / "app").exists() or not (root / "tests").exists():
                return _TestOutcome(ran=False, passed=0, failed=0, detail="no app/tests to run")

            # Prefer this interpreter's own `-m pytest`: it is guaranteed to be
            # the environment that actually has pytest/fastapi/httpx installed,
            # whereas a bare `pytest` on PATH may resolve to an unrelated one
            # (or none) depending on how this process was launched.
            args = [sys.executable, "-m", "pytest", "-q", "tests/test_api.py"]
            if shutil.which(sys.executable) is None:  # pragma: no cover - defensive
                args = ["pytest", "-q", "tests/test_api.py"]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=_subprocess_env(root),
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            except (OSError, TimeoutError) as exc:
                return _TestOutcome(ran=False, passed=0, failed=0, detail=str(exc))

            text = stdout.decode(errors="replace")
            passed, failed = _parse_pytest_summary(text)
            return _TestOutcome(ran=True, passed=passed, failed=failed, detail=text)

    def _pending_artifacts(self, state: RunState):
        """Artifacts this very stage is about to produce aren't in the
        workspace yet (the engine writes them only after this call returns),
        so the test run needs them injected directly."""
        return [
            ("tests/__init__.py", ""),
            ("tests/test_api.py", _test_api_py(
                (state.context.get("code", reader=self.stage_name) or {}).get("capabilities", {})
            )),
        ]

    def _render_report(self, outcome: _TestOutcome) -> str:
        status = f"{outcome.passed} passed, {outcome.failed} failed" if outcome.ran else "not run"
        lines = [
            "# Test Report",
            "",
            f"**Result:** {status}",
            "",
            "```",
            outcome.detail[-4000:],
            "```",
        ]
        return "\n".join(lines) + "\n"


class _TestOutcome:
    def __init__(self, *, ran: bool, passed: int, failed: int, detail: str) -> None:
        self.ran = ran
        self.passed = passed
        self.failed = failed
        self.detail = detail


def _subprocess_env(root: Path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["SHORTENER_DB_PATH"] = str(root / "test.db")
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _parse_pytest_summary(text: str) -> tuple[int, int]:
    """Pull passed/failed counts from pytest's final summary line, e.g.
    "3 passed in 0.12s" or "2 failed, 1 passed in 0.30s"."""
    import re

    passed = sum(int(m) for m in re.findall(r"(\d+) passed", text))
    failed = sum(int(m) for m in re.findall(r"(\d+) failed", text))
    errored = sum(int(m) for m in re.findall(r"(\d+) error", text))
    return passed, failed + errored


def _test_api_py(caps: dict) -> str:
    alias = caps.get("alias", False)
    expiry = caps.get("expiry", False)
    stats = caps.get("stats", False)

    alias_tests = '''

def test_custom_alias_is_used_verbatim_and_rejects_a_duplicate(client):
    payload1 = {"long_url": "https://example.com/a", "custom_alias": "mine"}
    r1 = client.post("/api/urls", json=payload1)
    assert r1.status_code == 201
    assert r1.json()["code"] == "mine"

    payload2 = {"long_url": "https://example.com/b", "custom_alias": "mine"}
    r2 = client.post("/api/urls", json=payload2)
    assert r2.status_code == 409
''' if alias else ""

    expiry_tests = '''

def test_expired_link_returns_410(client):
    # A past expires_at can no longer reach storage through the API (see
    # test_past_expires_at_is_rejected_at_creation below) -- this simulates
    # the case the redirect-time check actually exists for: a link that was
    # valid when created and has since passed its expiry, by inserting
    # directly through the storage layer rather than the (validating) API.
    from datetime import UTC, datetime, timedelta

    from app import storage

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    storage.insert(
        "expiredlink", "https://example.com/expired", datetime.now(UTC).isoformat(),
        expires_at=past,
    )
    got = client.get("/expiredlink", follow_redirects=False)
    assert got.status_code == 410


def test_past_expires_at_is_rejected_at_creation(client):
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    r = client.post(
        "/api/urls", json={"long_url": "https://example.com/expired", "expires_at": past}
    )
    assert r.status_code == 422


def test_future_expires_at_is_accepted_and_reported_back(client):
    from datetime import UTC, datetime, timedelta

    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    r = client.post(
        "/api/urls", json={"long_url": "https://example.com/future", "expires_at": future}
    )
    assert r.status_code == 201
    assert r.json()["expires_at"] is not None

    code = r.json()["code"]
    got = client.get(f"/{code}", follow_redirects=False)
    assert got.status_code == 302
''' if expiry else ""

    stats_tests = '''

def test_stats_reflect_click_count(client):
    r = client.post("/api/urls", json={"long_url": "https://example.com/tracked"})
    code = r.json()["code"]

    before = client.get(f"/api/urls/{code}/stats").json()
    assert before["click_count"] == 0

    client.get(f"/{code}", follow_redirects=False)
    client.get(f"/{code}", follow_redirects=False)

    after = client.get(f"/api/urls/{code}/stats").json()
    assert after["click_count"] == 2
''' if stats else ""

    return '''"""Integration tests for the generated URL shortener API.

Uses FastAPI's TestClient, which drives the real ASGI app in-process --
these exercise actual route/storage/codec wiring, not mocks.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTENER_DB_PATH", str(tmp_path / f"{uuid.uuid4().hex}.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_then_redirect(client):
    r = client.post("/api/urls", json={"long_url": "https://example.com/very/long/path"})
    assert r.status_code == 201
    body = r.json()
    assert body["long_url"] == "https://example.com/very/long/path"

    code = body["code"]
    got = client.get(f"/{code}", follow_redirects=False)
    assert got.status_code == 302
    assert got.headers["location"] == "https://example.com/very/long/path"


def test_unknown_code_is_404(client):
    assert client.get("/doesnotexist12345", follow_redirects=False).status_code == 404


def test_invalid_long_url_is_rejected(client):
    r = client.post("/api/urls", json={"long_url": "not-a-url"})
    assert r.status_code == 422


def test_delete_then_gone(client):
    r = client.post("/api/urls", json={"long_url": "https://example.com/to-delete"})
    code = r.json()["code"]
    assert client.delete(f"/api/urls/{code}").status_code == 204
    assert client.get(f"/{code}", follow_redirects=False).status_code == 404
''' + alias_tests + expiry_tests + stats_tests
