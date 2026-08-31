"""Tests for the `asdlc` CLI, exercised through Typer's CliRunner against a
real (deterministic-provider) run -- these are the same code paths a user
invokes from the terminal, not a mock of them.
"""


import pytest
from typer.testing import CliRunner

from orchestrator.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_run_root(tmp_path, monkeypatch):
    """Every test gets its own .asdlc run root so parallel test runs (and
    repeated invocations within one test) never share ledger/workspace state.
    """
    monkeypatch.setenv("ASDLC_RUN_ROOT", str(tmp_path / ".asdlc"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_build_materializes_a_runnable_service(tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "Build a URL shortener with core APIs, custom aliases, and click analytics.",
            "--out", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "materialized to" in result.output
    assert (out / "app" / "main.py").exists()
    assert (out / "tests" / "test_api.py").exists()
    assert (out / "requirements.txt").exists()
    assert (out / "README.md").exists()


def test_build_prints_stage_table_and_reliability_summary(tmp_path):
    result = runner.invoke(
        app,
        ["Build a URL shortener.", "--out", str(tmp_path / "out")],
    )
    assert "Stage Results" in result.output
    assert "reliability:" in result.output
    assert "requirements" in result.output
    assert "release" in result.output


def test_build_promote_replaces_stale_output(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale_leftover.py").write_text("# from a previous, unrelated run\n")

    result = runner.invoke(app, ["Build a URL shortener.", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert not (out / "stale_leftover.py").exists(), "promotion must fully replace, not merge"
    assert (out / "app" / "main.py").exists()


def test_ambiguous_kind_halts_and_promotes_nothing(tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "Make it better, TBD on details",
            "--kind", "ambiguous",
            "--out", str(out),
        ],
    )
    assert result.exit_code == 1
    assert not out.exists()
    assert "nothing promoted" in result.output


def test_brownfield_seed_is_reasoned_over(tmp_path):
    seed = tmp_path / "seed"
    (seed / "app").mkdir(parents=True)
    (seed / "app" / "redirect_handler.py").write_text("def redirect(code): ...\n")
    out = tmp_path / "out"

    result = runner.invoke(
        app,
        [
            "Fix a bug in the redirect handler.",
            "--kind", "brownfield",
            "--seed", str(seed),
            "--out", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "docs" / "architecture.md").exists()
    architecture_doc = (out / "docs" / "architecture.md").read_text()
    assert "redirect_handler.py" in architecture_doc


def test_repeated_builds_do_not_corrupt_the_ledger(tmp_path):
    """Regression test: run ids used to be derived only from --kind, so a
    second invocation would append fresh events onto a stale ledger file
    from the first, corrupting its hash chain."""
    for _ in range(2):
        result = runner.invoke(
            app, ["Build a URL shortener.", "--out", str(tmp_path / "out")]
        )
        assert result.exit_code == 0, result.output
