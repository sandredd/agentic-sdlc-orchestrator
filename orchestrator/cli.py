"""`asdlc`: the command-line entry point.

This is deliberately thin: every command builds the same `Engine` +
`build_graph()` + `build_agents()` wiring that Phase 4's agents and Phase 2's
engine already prove correct in isolation. Four commands, each a different
facet of controlled autonomy:

  build    -- run the full pipeline once, materialize the result.
  status   -- inspect a persisted run without touching it.
  resume   -- answer a pending human approval checkpoint and continue.
  clarify  -- answer a blocking ambiguity and retry from requirements.

`status`/`resume`/`clarify` all reload a run from exactly what `build` left
on disk (`state.json`, the ledger, the workspace) -- proving the run is
genuinely resumable across process boundaries, not just within one Python
call stack.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from orchestrator.agents import build_graph, make_executor
from orchestrator.config import AutonomyLevel, OrchestratorConfig
from orchestrator.contracts import Requirement, ScenarioKind, new_id
from orchestrator.core.approvals import (
    ApprovalResponse,
    AutoApproveProvider,
    CallbackApprovalProvider,
)
from orchestrator.core.engine import Engine
from orchestrator.core.ledger import Ledger
from orchestrator.core.metrics import ReliabilityReport
from orchestrator.core.state import RunState, RunStatus
from orchestrator.core.workspace import Workspace
from orchestrator.providers import get_provider

app = typer.Typer(
    help="Agentic SDLC orchestrator: turn a requirement into a reviewable engineering outcome.",
    add_completion=False,
)
console = Console()


@app.command()
def build(
    statement: str = typer.Argument(..., help="The requirement statement to build."),
    title: str = typer.Option("URL Shortener", "--title", help="Short title for the run."),
    kind: ScenarioKind = typer.Option(
        ScenarioKind.GREENFIELD.value, "--kind", help="greenfield | brownfield | ambiguous"
    ),
    out: Path = typer.Option(
        Path("target"), "--out", help="Directory to materialize the generated service into."
    ),
    autonomy: AutonomyLevel = typer.Option(
        AutonomyLevel.BOUNDED.value, "--autonomy",
        help="suggest | supervised | bounded | autonomous",
    ),
    auto_approve: bool = typer.Option(
        True, "--auto-approve/--no-auto-approve",
        help="Grant approval checkpoints automatically (demo mode). Off requires --interactive.",
    ),
    interactive: bool = typer.Option(
        False, "--interactive", help="Prompt on the terminal for each approval checkpoint."
    ),
    seed: Path = typer.Option(
        None, "--seed", help="Existing codebase directory to seed the workspace with "
        "(for a brownfield run)."
    ),
) -> None:
    """Run the full nine-stage SDLC pipeline and materialize the result into OUT.

    If the run halts on an approval checkpoint or a blocking ambiguity, its
    run id is printed; use `asdlc resume` or `asdlc clarify` with that id to
    continue it later without starting over.
    """
    exit_code = asyncio.run(
        _build(statement, title, kind, out, autonomy, auto_approve, interactive, seed)
    )
    raise typer.Exit(code=exit_code)


async def _build(
    statement: str,
    title: str,
    kind: ScenarioKind,
    out: Path,
    autonomy: AutonomyLevel,
    auto_approve: bool,
    interactive: bool,
    seed: Path | None,
) -> int:
    cfg = OrchestratorConfig.from_env(autonomy=autonomy)
    provider = get_provider(cfg)
    console.print(
        f"[bold]provider:[/bold] {provider.name}  [bold]autonomy:[/bold] {autonomy.value}"
    )

    # `new_id` rather than a fixed name derived from --kind: reusing a run id would
    # append fresh, seq-0-starting ledger events onto a previous run's ledger file,
    # corrupting its hash chain.
    run_id = new_id(f"cli-{ScenarioKind(kind).value}")
    console.print(f"[dim]run id: {run_id}[/dim]")

    # Constructed explicitly (never left for Engine's own `workspace or Workspace(...)`
    # fallback) so the exact same object is bound to both the engine and the agents --
    # otherwise an agent that needs to read the workspace (ArchitectureAgent for
    # brownfield reasoning, TestingAgent to actually run pytest) would be looking at
    # an empty stand-in while the engine writes artifacts somewhere else entirely.
    workspace = Workspace(
        cfg.run_root / run_id / "workspace", seed_from=seed if seed else None
    )
    graph = build_graph()
    executor = make_executor(provider, workspace=workspace)
    engine = Engine(graph, executor, config=cfg, workspace=workspace, run_id=run_id)
    _set_approval_provider(engine, auto_approve=auto_approve, interactive=interactive)

    req = Requirement(title=title, statement=statement, kind=kind)
    state = await engine.run(RunState(run_id=run_id, requirement=req))

    return _report_and_promote(engine, state, out)


@app.command()
def status(
    run_id: str = typer.Argument(..., help="A run id printed by a previous command."),
) -> None:
    """Show a persisted run's stage table, findings and any pending approvals."""
    engine, state = _reload(run_id)
    _print_stage_table(engine, state)
    _print_findings(state)
    _print_metrics(engine.metrics(state))
    pending = state.approval_log.pending()
    if pending:
        console.print("[yellow]pending approval(s):[/yellow]")
        for request in pending:
            console.print(request.brief())


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="A run id printed by a previous command."),
    approve: bool = typer.Option(
        True, "--approve/--reject", help="Grant or reject the pending approval checkpoint."
    ),
    note: str = typer.Option("", "--note", help="Reviewer note recorded with the decision."),
    out: Path = typer.Option(Path("target"), "--out"),
) -> None:
    """Answer a pending human approval checkpoint and continue a halted run."""
    exit_code = asyncio.run(_resume(run_id, approve, note, out))
    raise typer.Exit(code=exit_code)


async def _resume(run_id: str, approve: bool, note: str, out: Path) -> int:
    engine, state = _reload(run_id)
    if not state.approval_log.pending():
        console.print(f"[yellow]run {run_id} has no pending approval[/yellow]")
        return 1

    engine.approval_provider = CallbackApprovalProvider(
        lambda request: ApprovalResponse(
            request_id=request.id, granted=approve, approver="cli-user", note=note
        )
    )
    state = await engine.resume(state)
    return _report_and_promote(engine, state, out)


@app.command()
def clarify(
    run_id: str = typer.Argument(..., help="A run id printed by a previous command."),
    statement: str = typer.Argument(
        ..., help="The complete, clarified requirement statement -- replaces the original, "
        "it is not appended to it."
    ),
    out: Path = typer.Option(Path("target"), "--out"),
) -> None:
    """Replace a blocked run's requirement with a clarified statement and retry.

    Use this after `asdlc status` shows a blocking ambiguity: the requirements
    stage re-runs against the new statement, and every stage the ambiguity had
    blocked gets another chance once it produces an unambiguous normalized
    requirement. The new statement replaces the old one rather than appending
    to it, since a vague marker like "TBD" left in the text would otherwise
    keep tripping the same ambiguity check.
    """
    exit_code = asyncio.run(_clarify(run_id, statement, out))
    raise typer.Exit(code=exit_code)


async def _clarify(run_id: str, statement: str, out: Path) -> int:
    engine, state = _reload(run_id)
    if state.status is RunStatus.SUCCEEDED:
        console.print(f"[yellow]run {run_id} already succeeded; nothing to clarify[/yellow]")
        return 1

    engine.approval_provider = AutoApproveProvider()
    state.requirement = state.requirement.model_copy(update={"statement": statement})
    state = await engine.retry_stage(state, "requirements")
    return _report_and_promote(engine, state, out)


# -- shared plumbing ---------------------------------------------------------


def _set_approval_provider(engine: Engine, *, auto_approve: bool, interactive: bool) -> None:
    if interactive:
        engine.approval_provider = _TerminalApprovalProvider(console)
    elif auto_approve:
        engine.approval_provider = AutoApproveProvider()
    else:
        # `--no-auto-approve` without `--interactive`: a locked-down run with no
        # one to ask. Must not silently fall back to Engine's own default
        # provider, which is itself an AutoApproveProvider -- that would make
        # `--no-auto-approve` a no-op. Returning None from `decide` is the
        # documented "pending" signal, so the run genuinely halts and waits.
        engine.approval_provider = CallbackApprovalProvider(lambda request: None)


def _reload(run_id: str) -> tuple[Engine, RunState]:
    """Reconstruct the exact engine + state a previous `build`/`resume`/
    `clarify` left on disk: same workspace directory (not re-seeded -- its
    content *is* the state), same ledger file (loaded, not replaced, so its
    hash chain extends rather than restarts)."""
    cfg = OrchestratorConfig.from_env()
    provider = get_provider(cfg)
    run_dir = cfg.run_root / run_id
    state_path = run_dir / "state.json"
    if not state_path.exists():
        console.print(f"[red]no run found at {run_dir}[/red]")
        raise typer.Exit(code=1)

    state = RunState.load(state_path)
    workspace = Workspace(run_dir / "workspace")
    ledger = Ledger.load(run_id, path=run_dir / "ledger.jsonl")
    graph = build_graph()
    executor = make_executor(provider, workspace=workspace)
    engine = Engine(
        graph, executor, config=cfg, workspace=workspace, ledger=ledger, run_id=run_id
    )
    return engine, state


class _TerminalApprovalProvider(AutoApproveProvider):
    """Prompts on stdin for each checkpoint instead of auto-granting."""

    name = "terminal"

    def __init__(self, console: Console) -> None:
        self.console = console

    async def decide(self, request):
        self.console.print(request.brief())
        answer = typer.confirm("Approve?", default=True)
        note = typer.prompt("Note (optional)", default="", show_default=False)
        return ApprovalResponse(
            request_id=request.id, granted=answer, approver="cli-user", note=note
        )


def _promote(workspace: Workspace, out: Path) -> None:
    """Copy the run's workspace into `out`, replacing whatever was there.

    A full replace rather than a merge: the workspace is the complete,
    self-consistent output of one run, and merging it with stale files from a
    previous run is exactly the kind of half-applied state this project's
    engine goes out of its way to avoid elsewhere.
    """
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(
        workspace.root, out, ignore=shutil.ignore_patterns("__pycache__", "*.db", "*.pyc")
    )


def _report_and_promote(engine: Engine, state: RunState, out: Path) -> int:
    _print_stage_table(engine, state)
    _print_findings(state)
    _print_metrics(engine.metrics(state))

    if state.status is not RunStatus.SUCCEEDED:
        console.print(
            f"[yellow]run ended in {state.status.value} "
            f"({state.halt_reason.value if state.halt_reason else 'n/a'}); "
            f"nothing promoted -- run id: {state.run_id}[/yellow]"
        )
        return 1

    _promote(engine.workspace, out)
    console.print(f"[green]materialized to {out}/[/green] -- see {out}/README.md to run it")
    return 0


def _print_stage_table(engine: Engine, state: RunState) -> None:
    table = Table(title="Stage Results")
    table.add_column("stage")
    table.add_column("status")
    table.add_column("duration")
    for name in engine.graph.topological_order():
        st = state.stage(name)
        duration = f"{st.duration_seconds:.3f}s" if st.duration_seconds is not None else "-"
        table.add_row(name, st.status.value, duration)
    console.print(table)


def _print_findings(state: RunState) -> None:
    if not state.findings:
        return
    table = Table(title="Findings")
    table.add_column("severity")
    table.add_column("category")
    table.add_column("summary")
    for f in sorted(state.findings, key=lambda f: -f.severity.rank):
        table.add_row(f.severity.value, f.category, f.summary)
    console.print(table)


def _print_metrics(report: ReliabilityReport) -> None:
    console.print(f"[bold]reliability:[/bold] {report.summary_line()}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
