"""Sandboxed run workspace with snapshot/restore.

Agents are not trusted to write wherever they like. Every artifact write goes
through :class:`Workspace`, which:

* confines all paths under the run root (rejecting ``..`` traversal, absolute
  paths and symlinks that resolve outside the sandbox), and
* records content-addressed snapshots so any stage's writes can be rolled back
  to a known-good point.

Snapshots capture file *content*, not just a diff, because a rollback has to
work even when an agent rewrote a file it never read.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.contracts import Artifact, new_id


class SandboxViolation(Exception):
    """Raised when a write would land outside the workspace root."""


@dataclass(frozen=True)
class Snapshot:
    """Full content capture of the workspace at a point in time."""

    id: str
    label: str
    files: dict[str, str] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        joined = "\n".join(
            f"{path}:{hashlib.sha256(content.encode()).hexdigest()}"
            for path, content in sorted(self.files.items())
        )
        return hashlib.sha256(joined.encode()).hexdigest()


class Workspace:
    """A per-run filesystem sandbox."""

    def __init__(self, root: Path, *, seed_from: Path | None = None) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._snapshots: dict[str, Snapshot] = {}
        if seed_from is not None:
            self.seed(seed_from)

    # -- path safety -------------------------------------------------------

    def resolve(self, relative: str) -> Path:
        """Resolve a workspace-relative path, refusing anything that escapes.

        ``strict=False`` lets us validate paths that do not exist yet; the
        containment check runs on the resolved form either way, so a symlink
        pointing outside the sandbox is caught before it is written through.
        """
        if Path(relative).is_absolute():
            raise SandboxViolation(f"absolute paths are not writable: {relative!r}")
        candidate = (self.root / relative).resolve(strict=False)
        if candidate != self.root and self.root not in candidate.parents:
            raise SandboxViolation(f"path escapes the workspace sandbox: {relative!r}")
        return candidate

    # -- io ----------------------------------------------------------------

    def write(self, relative: str, content: str) -> Path:
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def write_artifact(self, artifact: Artifact) -> Artifact:
        sealed = artifact.with_hash()
        self.write(sealed.path, sealed.content)
        return sealed

    def read(self, relative: str) -> str:
        return self.resolve(relative).read_text(encoding="utf-8")

    def exists(self, relative: str) -> bool:
        try:
            return self.resolve(relative).exists()
        except SandboxViolation:
            return False

    def files(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.root))
            for p in self.root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )

    def seed(self, source: Path) -> None:
        """Copy an existing tree in. This is how a brownfield run gets the
        codebase it is meant to reason about and modify."""
        source = source.resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"seed source is not a directory: {source}")
        for path in source.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(source)
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    # -- snapshots ---------------------------------------------------------

    def snapshot(self, label: str) -> Snapshot:
        snap = Snapshot(
            id=new_id("snap"),
            label=label,
            files={rel: self.read(rel) for rel in self.files()},
        )
        self._snapshots[snap.id] = snap
        return snap

    def restore(self, snapshot: Snapshot | str) -> list[str]:
        """Restore the workspace to a snapshot. Returns the paths that changed.

        Files created after the snapshot are deleted, not merely left behind —
        a partial rollback that leaves an orphaned module is worse than none.
        """
        snap = self._snapshots[snapshot] if isinstance(snapshot, str) else snapshot
        changed: list[str] = []

        for rel in self.files():
            if rel not in snap.files:
                self.resolve(rel).unlink()
                changed.append(rel)

        for rel, content in snap.files.items():
            if not self.exists(rel) or self.read(rel) != content:
                self.write(rel, content)
                changed.append(rel)

        self._prune_empty_dirs()
        return sorted(set(changed))

    def _prune_empty_dirs(self) -> None:
        for path in sorted(self.root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()

    @property
    def snapshots(self) -> dict[str, Snapshot]:
        return dict(self._snapshots)
