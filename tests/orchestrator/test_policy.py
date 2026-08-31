from orchestrator.contracts import (
    Artifact,
    ArtifactKind,
    ScenarioKind,
    Severity,
    StageResult,
)
from orchestrator.core.policy import (
    ChangeControlPolicy,
    ChangeControlRule,
    NoDangerousConstructsRule,
    NoHardcodedSecretsRule,
    NoPiiInLogsRule,
    PolicyContext,
    PolicyEngine,
    SqlInjectionRule,
    TestsAccompanyCodeRule,
)

from .conftest import fresh_state, stage


def code(path: str, content: str) -> Artifact:
    return Artifact(path=path, kind=ArtifactKind.CODE, content=content)


def ctx(*artifacts: Artifact, state=None) -> PolicyContext:
    return PolicyContext(
        node=stage("impl"),
        state=state or fresh_state(),
        result=StageResult(stage="impl", artifacts=artifacts),
    )


# -- secrets -----------------------------------------------------------


def test_flags_aws_key():
    findings = NoHardcodedSecretsRule().evaluate(
        ctx(code("a.py", 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n'))
    )
    assert findings and findings[0].severity is Severity.BLOCKER


def test_flags_private_key_block():
    content = "x = 1\n-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----\n"
    findings = NoHardcodedSecretsRule().evaluate(ctx(code("k.pem", content)))
    assert findings


def test_flags_password_literal():
    findings = NoHardcodedSecretsRule().evaluate(
        ctx(code("a.py", 'password = "hunter2-actual-value"\n'))
    )
    assert findings


def test_flags_credentialed_connection_string():
    findings = NoHardcodedSecretsRule().evaluate(
        ctx(code("a.py", 'DSN = "postgres://admin:S3cretPass@db.internal:5432/app"\n'))
    )
    assert findings


def test_does_not_flag_env_based_config():
    findings = NoHardcodedSecretsRule().evaluate(
        ctx(code("a.py", 'password = os.environ["DB_PASSWORD"]\n'))
    )
    assert findings == []


def test_does_not_flag_placeholder_values():
    findings = NoHardcodedSecretsRule().evaluate(
        ctx(code("a.py", 'api_key = "<your-api-key-here>"\n'))
    )
    assert findings == []


def test_does_not_flag_short_benign_strings():
    findings = NoHardcodedSecretsRule().evaluate(ctx(code("a.py", 'name = "shortener"\n')))
    assert findings == []


# -- dangerous constructs ------------------------------------------------


def test_flags_eval_and_exec():
    findings = NoDangerousConstructsRule().evaluate(
        ctx(code("a.py", "eval(user_input)\nexec(payload)\n"))
    )
    assert len(findings) == 2
    assert all(f.severity is Severity.BLOCKER for f in findings)


def test_flags_shell_true():
    findings = NoDangerousConstructsRule().evaluate(
        ctx(code("a.py", "subprocess.run(cmd, shell=True)\n"))
    )
    assert findings and findings[0].severity is Severity.HIGH


def test_ignores_commented_lines():
    findings = NoDangerousConstructsRule().evaluate(
        ctx(code("a.py", "# eval(x) is dangerous, do not do this\n"))
    )
    assert findings == []


def test_does_not_flag_normal_code():
    findings = NoDangerousConstructsRule().evaluate(
        ctx(code("a.py", "def add(a, b):\n    return a + b\n"))
    )
    assert findings == []


def test_ignores_non_code_artifacts():
    doc = Artifact(path="README.md", kind=ArtifactKind.DOC, content="eval() is bad\n")
    findings = NoDangerousConstructsRule().evaluate(ctx(doc))
    assert findings == []


# -- SQL injection --------------------------------------------------------


def test_flags_fstring_sql():
    findings = SqlInjectionRule().evaluate(
        ctx(code("a.py", 'cursor.execute(f"SELECT * FROM users WHERE id={uid}")\n'))
    )
    assert findings and findings[0].severity is Severity.BLOCKER


def test_does_not_flag_parameterised_sql():
    findings = SqlInjectionRule().evaluate(
        ctx(code("a.py", 'cursor.execute("SELECT * FROM users WHERE id=%s", (uid,))\n'))
    )
    assert findings == []


# -- PII in logs -----------------------------------------------------------


def test_flags_pii_in_log_statement():
    findings = NoPiiInLogsRule().evaluate(
        ctx(code("a.py", 'logger.info(f"user email={user.email}")\n'))
    )
    assert findings


def test_does_not_flag_opaque_identifier_logging():
    findings = NoPiiInLogsRule().evaluate(
        ctx(code("a.py", 'logger.info(f"processed user_id={user.id}")\n'))
    )
    assert findings == []


# -- tests-accompany-code ---------------------------------------------------


def test_flags_code_with_no_tests_anywhere():
    findings = TestsAccompanyCodeRule().evaluate(ctx(code("app/x.py", "x = 1\n")))
    assert findings and findings[0].severity is Severity.MEDIUM


def test_does_not_flag_when_this_result_includes_tests():
    test_art = Artifact(path="tests/test_x.py", kind=ArtifactKind.TEST, content="def test_x(): ...")
    findings = TestsAccompanyCodeRule().evaluate(ctx(code("app/x.py", "x = 1\n"), test_art))
    assert findings == []


def test_does_not_flag_when_tests_already_exist_in_state():
    state = fresh_state()
    state.artifacts["tests/test_x.py"] = Artifact(
        path="tests/test_x.py", kind=ArtifactKind.TEST, content="def test_x(): ..."
    )
    findings = TestsAccompanyCodeRule().evaluate(ctx(code("app/y.py", "y = 1\n"), state=state))
    assert findings == []


# -- change control ----------------------------------------------------------


def test_flags_write_to_protected_glob():
    rule = ChangeControlRule(ChangeControlPolicy(protected_globs=("**/migrations/**",)))
    findings = rule.evaluate(ctx(code("db/migrations/0001_init.sql", "CREATE TABLE x;\n")))
    assert findings and findings[0].severity is Severity.HIGH


def test_flags_write_to_frozen_glob_as_blocker():
    rule = ChangeControlRule(ChangeControlPolicy(frozen_globs=("**/.github/**",)))
    findings = rule.evaluate(ctx(code(".github/workflows/ci.yml", "name: ci\n")))
    assert findings and findings[0].severity is Severity.BLOCKER


def test_flags_too_many_files():
    rule = ChangeControlRule(ChangeControlPolicy(max_files_per_stage=2))
    artifacts = tuple(code(f"a{i}.py", "x = 1\n") for i in range(5))
    findings = rule.evaluate(ctx(*artifacts))
    assert any("touched 5 files" in f.summary for f in findings)


def test_flags_oversized_artifact():
    rule = ChangeControlRule(ChangeControlPolicy(max_lines_per_artifact=3))
    findings = rule.evaluate(ctx(code("big.py", "x = 1\n" * 10)))
    assert any("over 3" in f.summary for f in findings)


def test_normal_change_passes_clean():
    rule = ChangeControlRule()
    findings = rule.evaluate(ctx(code("app/shortener.py", "def shorten(u): ...\n")))
    assert findings == []


# -- engine-level pass -------------------------------------------------------


def test_policy_engine_runs_all_rules_in_one_pass():
    engine = PolicyEngine.default()
    result = StageResult(
        stage="impl",
        artifacts=(code("a.py", 'AWS = "AKIAABCDEFGHIJKLMNOP"\neval(x)\n'),),
    )
    findings = engine.evaluate(stage("impl"), fresh_state(), result)
    codes = {f.summary.split("]")[0][1:] for f in findings}
    assert "SEC001" in codes
    assert "SEC002" in codes


def test_blockers_filters_correctly():
    engine = PolicyEngine.default()
    result = StageResult(stage="impl", artifacts=(code("a.py", "x = 1\n"),))
    findings = engine.evaluate(stage("impl"), fresh_state(), result)
    assert PolicyEngine.blockers(findings) == [
        f for f in findings if f.severity is Severity.BLOCKER
    ]


def test_broken_rule_does_not_take_down_evaluation():
    class Explodes:
        code = "BAD001"

        def evaluate(self, ctx):
            raise RuntimeError("boom")

    engine = PolicyEngine(rules=[Explodes()])
    findings = engine.evaluate(stage("impl"), fresh_state(), StageResult(stage="impl"))
    assert len(findings) == 1
    assert findings[0].severity is Severity.LOW


def test_brownfield_overwrite_without_provenance_is_flagged():
    state = fresh_state(ScenarioKind.BROWNFIELD)
    old = code("app/x.py", "old\n").with_hash()
    state.artifacts["app/x.py"] = old
    new = code("app/x.py", "new\n")
    findings = PolicyEngine.default().evaluate(
        stage("impl"), state, StageResult(stage="impl", artifacts=(new,))
    )
    assert any(f.category == "change_control" and "supersedes" in f.summary for f in findings)


def test_greenfield_overwrite_is_not_flagged_by_brownfield_rule():
    state = fresh_state(ScenarioKind.GREENFIELD)
    old = code("app/x.py", "old\n").with_hash()
    state.artifacts["app/x.py"] = old
    new = code("app/x.py", "new\n")
    findings = PolicyEngine.default().evaluate(
        stage("impl"), state, StageResult(stage="impl", artifacts=(new,))
    )
    assert not any("supersedes" in f.summary for f in findings)
