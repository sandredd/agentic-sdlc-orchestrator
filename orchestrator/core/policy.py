"""Policy guardrails for security, compliance and change control.

Design note: policy is evaluated by the *engine*, on every stage result,
rather than being attached to nodes as an opt-in gate. A guardrail you can
forget to wire up is not a guardrail. Gates express what a *particular* stage
must satisfy; policy expresses what *no* stage may do.

Each rule returns :class:`Finding` objects rather than raising, so a single
pass reports every violation and the engine decides severity handling. A
BLOCKER makes the stage result inadmissible; anything lower is recorded and
carried into the risk register.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from orchestrator.contracts import (
    ArtifactKind,
    Finding,
    ScenarioKind,
    Severity,
    StageResult,
)

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState


@dataclass(frozen=True)
class PolicyContext:
    """What a rule is allowed to see. Deliberately narrow, so rules stay pure
    and unit-testable without standing up a whole run."""

    node: StageNode
    state: RunState
    result: StageResult


class PolicyRule(ABC):
    code: str = "POL000"
    title: str = ""
    category: str = "policy"
    rationale: str = ""

    @abstractmethod
    def evaluate(self, ctx: PolicyContext) -> list[Finding]: ...

    def finding(
        self,
        severity: Severity,
        summary: str,
        *,
        detail: str = "",
        path: str | None = None,
        remediation: str | None = None,
    ) -> Finding:
        return Finding(
            severity=severity,
            category=self.category,
            summary=f"[{self.code}] {summary}",
            detail=detail or self.rationale,
            path=path,
            raised_by=f"policy:{self.code}",
            remediation=remediation,
        )


# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------

# Deliberately narrow patterns. A secret scanner that cries wolf gets disabled,
# which is strictly worse than one with a few gaps.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("generic API key literal", re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\b"
        r"\s*[=:]\s*['\"][^'\"]{12,}['\"]"
    )),
    ("password literal", re.compile(
        r"(?i)\b(?:password|passwd|pwd)\b\s*[=:]\s*['\"][^'\"]{6,}['\"]"
    )),
    ("connection string with credentials", re.compile(
        r"(?i)\b(?:postgres|postgresql|mysql|mongodb|redis|amqp)(?:\+\w+)?://"
        r"[^\s:'\"/]+:[^\s@'\"]+@"
    )),
)

# Values that look like secrets but are obviously placeholders. Matching these
# is the difference between a usable scanner and an ignored one.
_PLACEHOLDER = re.compile(
    r"(?i)(?:\bos\.environ\b|\bgetenv\b|\bsettings\.|<[^>]{2,}>|\$\{|\{\{"
    r"|\bchangeme\b|\bplaceholder\b|\bredacted\b|\bexample\b|\bdummy\b"
    r"|\bxxx+\b|\bfake\b|\byour[_-]|\bTODO\b)"
)


class NoHardcodedSecretsRule(PolicyRule):
    code = "SEC001"
    title = "No hardcoded secrets in generated artifacts"
    category = "security"
    rationale = (
        "A credential committed to a repository must be treated as compromised. "
        "Blocking at generation time is orders of magnitude cheaper than rotating it."
    )

    def evaluate(self, ctx: PolicyContext) -> list[Finding]:
        findings = []
        for artifact in ctx.result.artifacts:
            for line_no, line in enumerate(artifact.content.splitlines(), start=1):
                if _PLACEHOLDER.search(line):
                    continue
                for label, pattern in _SECRET_PATTERNS:
                    if pattern.search(line):
                        findings.append(
                            self.finding(
                                Severity.BLOCKER,
                                f"possible {label} at {artifact.path}:{line_no}",
                                path=artifact.path,
                                remediation="read the value from configuration or a secret store",
                            )
                        )
                        break
        return findings


_DANGEROUS = (
    (re.compile(r"\beval\s*\("), "eval()", Severity.BLOCKER),
    (re.compile(r"(?<!\.)\bexec\s*\("), "exec()", Severity.BLOCKER),
    (re.compile(r"\bos\.system\s*\("), "os.system()", Severity.BLOCKER),
    (re.compile(r"shell\s*=\s*True"), "subprocess with shell=True", Severity.HIGH),
    (re.compile(r"\bpickle\.loads?\s*\("), "pickle deserialisation", Severity.HIGH),
    (re.compile(r"\byaml\.load\s*\((?![^)]*Loader)"), "yaml.load without a safe Loader",
     Severity.HIGH),
    (re.compile(r"verify\s*=\s*False"), "TLS verification disabled", Severity.HIGH),
    (re.compile(r"(?i)\bmd5\b|\bsha1\b"), "weak hash primitive", Severity.MEDIUM),
)


class NoDangerousConstructsRule(PolicyRule):
    code = "SEC002"
    title = "No unsafe language constructs in generated code"
    category = "security"
    rationale = "Agent-generated code is not exempt from the review bar applied to humans."

    def evaluate(self, ctx: PolicyContext) -> list[Finding]:
        findings = []
        for artifact in ctx.result.artifacts:
            if artifact.kind not in {ArtifactKind.CODE, ArtifactKind.TEST}:
                continue
            for line_no, line in enumerate(artifact.content.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern, label, severity in _DANGEROUS:
                    if pattern.search(line):
                        findings.append(
                            self.finding(
                                severity,
                                f"{label} at {artifact.path}:{line_no}",
                                path=artifact.path,
                                remediation=f"replace {label} with a safe equivalent",
                            )
                        )
        return findings


class SqlInjectionRule(PolicyRule):
    code = "SEC003"
    title = "SQL must be parameterised"
    category = "security"
    rationale = "String-built SQL is the most common injection vector in CRUD services."

    _INTERPOLATED_SQL = re.compile(
        r"(?i)(?:execute|executemany|cursor\.execute|text)\s*\(\s*"
        r"(?:f['\"]|['\"][^'\"]*['\"]\s*(?:%|\+|\.format))"
    )

    def evaluate(self, ctx: PolicyContext) -> list[Finding]:
        findings = []
        for artifact in ctx.result.artifacts:
            if artifact.kind is not ArtifactKind.CODE:
                continue
            for line_no, line in enumerate(artifact.content.splitlines(), start=1):
                if self._INTERPOLATED_SQL.search(line):
                    findings.append(
                        self.finding(
                            Severity.BLOCKER,
                            f"SQL built by string interpolation at {artifact.path}:{line_no}",
                            path=artifact.path,
                            remediation="use bound parameters",
                        )
                    )
        return findings


# --------------------------------------------------------------------------
# Compliance
# --------------------------------------------------------------------------


class NoPiiInLogsRule(PolicyRule):
    code = "CMP001"
    title = "No personal data written to logs"
    category = "compliance"
    rationale = "Log sinks are rarely in scope for data-retention controls; keep PII out of them."

    _LOGGING = re.compile(
        r"(?i)\b(?:logger|logging|log)\s*\.\s*"
        r"(?:debug|info|warning|error|critical)\b|\bprint\s*\("
    )
    _PII = re.compile(
        r"(?i)\b(?:email|e_mail|ssn|social_security|password|passwd|token|secret"
        r"|credit_card|card_number|cvv|dob|date_of_birth|phone_number|ip_address)\b"
    )

    def evaluate(self, ctx: PolicyContext) -> list[Finding]:
        findings = []
        for artifact in ctx.result.artifacts:
            if artifact.kind is not ArtifactKind.CODE:
                continue
            for line_no, line in enumerate(artifact.content.splitlines(), start=1):
                if self._LOGGING.search(line) and self._PII.search(line):
                    findings.append(
                        self.finding(
                            Severity.HIGH,
                            f"potential PII in a log statement at {artifact.path}:{line_no}",
                            path=artifact.path,
                            remediation="log an opaque identifier instead of the value",
                        )
                    )
        return findings


class TestsAccompanyCodeRule(PolicyRule):
    code = "CMP002"
    title = "Production code ships with tests"
    category = "compliance"
    rationale = "Untested generated code transfers the verification burden to the reviewer."

    def evaluate(self, ctx: PolicyContext) -> list[Finding]:
        code = [a for a in ctx.result.artifacts if a.kind is ArtifactKind.CODE]
        if not code:
            return []
        # Tests may legitimately arrive from a later stage; only the stage that
        # declares itself as producing code-plus-tests is held to this here.
        has_tests = any(a.kind is ArtifactKind.TEST for a in ctx.result.artifacts)
        already_tested = any(
            a.kind is ArtifactKind.TEST for a in ctx.state.artifacts.values()
        )
        if has_tests or already_tested:
            return []
        return [
            self.finding(
                Severity.MEDIUM,
                f"{len(code)} code artifact(s) produced with no accompanying tests",
                remediation="add unit tests, or let the downstream test stage cover them",
            )
        ]


# --------------------------------------------------------------------------
# Change control
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeControlPolicy:
    """Which paths may be modified, and by whom.

    Brownfield work is where an agent does the most damage: rewriting a
    migration or a security module while nominally "refactoring". Protected
    paths make that an explicit, approvable decision rather than a silent one.
    """

    protected_globs: tuple[str, ...] = (
        "**/migrations/**",
        "**/alembic/**",
        "**/.github/**",
        "**/secrets/**",
        "**/*.lock",
    )
    frozen_globs: tuple[str, ...] = ()
    max_files_per_stage: int = 25
    max_lines_per_artifact: int = 1500


class ChangeControlRule(PolicyRule):
    code = "CHG001"
    title = "Changes stay within the declared blast radius"
    category = "change_control"
    rationale = (
        "Bounding the size and reach of a single stage's change is what keeps a "
        "bad generation reviewable and revertible."
    )

    def __init__(self, policy: ChangeControlPolicy | None = None) -> None:
        self.policy = policy or ChangeControlPolicy()

    @staticmethod
    def _matches(path: str, pattern: str) -> bool:
        """fnmatch's `**` is just `*` (it has no notion of path segments), so
        a leading ``**/`` never matches a path with nothing before it -- e.g.
        ``**/.github/**`` would silently miss a root-level ``.github/...``.
        Checking the pattern both with and without that leading segment closes
        the gap without pulling in a full glob library.
        """
        from fnmatch import fnmatch

        if fnmatch(path, pattern):
            return True
        stripped = pattern.removeprefix("**/")
        return stripped != pattern and fnmatch(path, stripped)

    def evaluate(self, ctx: PolicyContext) -> list[Finding]:
        findings: list[Finding] = []
        artifacts = ctx.result.artifacts

        if len(artifacts) > self.policy.max_files_per_stage:
            findings.append(
                self.finding(
                    Severity.HIGH,
                    f"stage touched {len(artifacts)} files, over the limit of "
                    f"{self.policy.max_files_per_stage}",
                    remediation="decompose into smaller stages so each change stays reviewable",
                )
            )

        for artifact in artifacts:
            for pattern in self.policy.frozen_globs:
                if self._matches(artifact.path, pattern):
                    findings.append(
                        self.finding(
                            Severity.BLOCKER,
                            f"write to frozen path {artifact.path}",
                            path=artifact.path,
                            remediation="frozen paths are out of scope for automated change",
                        )
                    )
                    break
            else:
                for pattern in self.policy.protected_globs:
                    if self._matches(artifact.path, pattern):
                        findings.append(
                            self.finding(
                                Severity.HIGH,
                                f"write to protected path {artifact.path} requires sign-off",
                                path=artifact.path,
                                remediation="route through a human approval checkpoint",
                            )
                        )
                        break

            lines = artifact.content.count("\n") + 1
            if lines > self.policy.max_lines_per_artifact:
                findings.append(
                    self.finding(
                        Severity.MEDIUM,
                        f"{artifact.path} is {lines} lines, over "
                        f"{self.policy.max_lines_per_artifact}",
                        path=artifact.path,
                        remediation="split the module",
                    )
                )
        return findings


class BrownfieldOverwriteRule(PolicyRule):
    code = "CHG002"
    title = "Existing files are modified deliberately"
    category = "change_control"
    rationale = (
        "In a brownfield run, silently replacing a file the agent never read is "
        "how unrelated behaviour gets destroyed."
    )

    def evaluate(self, ctx: PolicyContext) -> list[Finding]:
        if ctx.state.requirement.kind is not ScenarioKind.BROWNFIELD:
            return []
        findings = []
        for artifact in ctx.result.artifacts:
            previous = ctx.state.artifacts.get(artifact.path)
            if previous is None or previous.content_hash == artifact.content_hash:
                continue
            if artifact.supersedes != previous.id:
                findings.append(
                    self.finding(
                        Severity.MEDIUM,
                        f"{artifact.path} overwritten without declaring what it supersedes",
                        path=artifact.path,
                        remediation="set Artifact.supersedes so the change has a provenance link",
                    )
                )
        return findings


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


@dataclass
class PolicyEngine:
    """Runs every rule over a stage result in one pass."""

    rules: list[PolicyRule] = field(default_factory=list)

    @classmethod
    def default(cls, change_control: ChangeControlPolicy | None = None) -> PolicyEngine:
        return cls(
            rules=[
                NoHardcodedSecretsRule(),
                NoDangerousConstructsRule(),
                SqlInjectionRule(),
                NoPiiInLogsRule(),
                TestsAccompanyCodeRule(),
                ChangeControlRule(change_control),
                BrownfieldOverwriteRule(),
            ]
        )

    def evaluate(self, node: StageNode, state: RunState, result: StageResult) -> list[Finding]:
        ctx = PolicyContext(node=node, state=state, result=result)
        findings: list[Finding] = []
        for rule in self.rules:
            try:
                findings.extend(rule.evaluate(ctx))
            except Exception as exc:  # a broken rule must not take the run down
                findings.append(
                    Finding(
                        severity=Severity.LOW,
                        category="policy",
                        summary=f"[{rule.code}] policy rule failed to evaluate",
                        detail=f"{type(exc).__name__}: {exc}",
                        raised_by=f"policy:{rule.code}",
                    )
                )
        return findings

    @staticmethod
    def blockers(findings: list[Finding]) -> list[Finding]:
        return [f for f in findings if f.severity is Severity.BLOCKER]

    @property
    def codes(self) -> list[str]:
        return [r.code for r in self.rules]
