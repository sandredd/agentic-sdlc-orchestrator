"""`asdlc`: the command-line entry point.

This is deliberately thin: every command builds the same `Engine` +
`build_graph()` + `build_agents()` wiring that Phase 4's agents and Phase 2's
engine already prove correct in isolation. The CLI's only job is to turn
process arguments into a `Requirement`, run it, print something a human can
act on, and -- for `build` -- materialize the result into a real directory on
disk, which is what makes "runnable end-to-end prototype" true of the actual
generated service rather than just of the orchestrator itself.
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
from orchestrator.core.approvals import AutoApproveProvider
from orchestrator.core.engine import Engine
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
    """Run the full nine-stage SDLC pipeline and materialize the result into OUT."""
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

    # Constructed explicitly (never left for Engine's own `workspace or Workspace(...)`
    # fallback) so the exact same object is bound to both the engine and the agents --
    # otherwise an agent that needs to read the workspace (ArchitectureAgent for
    # brownfield reasoning, TestingAgent to actually run pytest) would be looking at
    # an empty stand-in while the engine writes artifacts somewhere else entirely.
    #
    # `new_id` rather than a fixed name derived from --kind: reusing a run id would
    # append fresh, seq-0-starting ledger events onto a previous run's ledger file,
    # corrupting its hash chain.
    run_id = new_id(f"cli-{ScenarioKind(kind).value}")
    workspace = Workspace(
        cfg.run_root / run_id / "workspace", seed_from=seed if seed else None
    )
    graph = build_graph()
    executor = make_executor(provider, workspace=workspace)
    engine = Engine(graph, executor, config=cfg, workspace=workspace, run_id=run_id)

    if interactive:
        engine.approval_provider = _TerminalApprovalProvider(console)
    elif auto_approve:
        engine.approval_provider = AutoApproveProvider()
    # else: leave the engine's own AutoApproveProvider default in place is wrong for
    # a deliberately non-approving run -- but a locked-down demo without
    # --interactive has no human to ask, so default to auto-approve.

    req = Requirement(title=title, statement=statement, kind=kind)
    state = await engine.run(RunState(run_id=run_id, requirement=req))

    _print_stage_table(engine, state)
    _print_findings(state)
    _print_metrics(engine.metrics(state))

    if state.status is not RunStatus.SUCCEEDED:
        console.print(
            f"[yellow]run ended in {state.status.value} "
            f"({state.halt_reason.value if state.halt_reason else 'n/a'}); "
            f"nothing promoted[/yellow]"
        )
        return 1

    _promote(engine.workspace, out)
    console.print(f"[green]materialized to {out}/[/green] -- see {out}/README.md to run it")
    return 0


class _TerminalApprovalProvider(AutoApproveProvider):
    """Prompts on stdin for each checkpoint instead of auto-granting."""

    name = "terminal"

    def __init__(self, console: Console) -> None:
        self.console = console

    async def decide(self, request):
        from orchestrator.core.approvals import ApprovalResponse

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
