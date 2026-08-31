from pathlib import Path

import pytest

from orchestrator.contracts import Artifact, ArtifactKind
from orchestrator.core.workspace import SandboxViolation, Workspace


def test_write_and_read(tmp_path: Path):
    ws = Workspace(tmp_path / "run")
    ws.write("src/app.py", "print('hi')\n")
    assert ws.read("src/app.py") == "print('hi')\n"
    assert ws.files() == ["src/app.py"]


@pytest.mark.parametrize(
    "bad",
    ["../escape.py", "a/../../escape.py", "/etc/passwd", "src/../../nope.txt"],
)
def test_traversal_is_refused(tmp_path: Path, bad: str):
    ws = Workspace(tmp_path / "run")
    with pytest.raises(SandboxViolation):
        ws.write(bad, "pwned")


def test_symlink_escape_is_refused(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = Workspace(tmp_path / "run")
    (ws.root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SandboxViolation):
        ws.write("link/stolen.txt", "pwned")
    assert not (outside / "stolen.txt").exists()


def test_write_artifact_seals_hash(tmp_path: Path):
    ws = Workspace(tmp_path / "run")
    art = Artifact(path="api/spec.yaml", kind=ArtifactKind.API_SPEC, content="openapi: 3.1.0\n")
    assert art.content_hash == ""

    sealed = ws.write_artifact(art)
    assert len(sealed.content_hash) == 64
    assert ws.read("api/spec.yaml") == "openapi: 3.1.0\n"


def test_snapshot_restore_reverts_modification_and_creation(tmp_path: Path):
    ws = Workspace(tmp_path / "run")
    ws.write("keep.py", "original\n")
    snap = ws.snapshot("before-impl")

    ws.write("keep.py", "agent rewrote this\n")
    ws.write("pkg/new_module.py", "half-finished\n")

    changed = ws.restore(snap)

    assert ws.read("keep.py") == "original\n"
    assert not ws.exists("pkg/new_module.py"), "rollback must not leave orphans"
    assert set(changed) == {"keep.py", "pkg/new_module.py"}
    assert ws.files() == ["keep.py"]


def test_restore_is_idempotent(tmp_path: Path):
    ws = Workspace(tmp_path / "run")
    ws.write("a.py", "one\n")
    snap = ws.snapshot("s")
    ws.write("a.py", "two\n")

    assert ws.restore(snap) == ["a.py"]
    assert ws.restore(snap) == [], "restoring an already-restored snapshot changes nothing"


def test_restore_by_id(tmp_path: Path):
    ws = Workspace(tmp_path / "run")
    ws.write("a.py", "one\n")
    snap = ws.snapshot("s")
    ws.write("a.py", "two\n")
    ws.restore(snap.id)
    assert ws.read("a.py") == "one\n"


def test_seed_from_existing_tree(tmp_path: Path):
    source = tmp_path / "legacy"
    (source / "svc").mkdir(parents=True)
    (source / "svc" / "handler.py").write_text("def handle(): ...\n")
    (source / "README.md").write_text("# legacy\n")
    (source / "svc" / "__pycache__").mkdir()
    (source / "svc" / "__pycache__" / "junk.pyc").write_bytes(b"\x00")

    ws = Workspace(tmp_path / "run", seed_from=source)

    assert ws.files() == ["README.md", "svc/handler.py"]
    assert "def handle" in ws.read("svc/handler.py")


def test_snapshot_fingerprint_tracks_content(tmp_path: Path):
    ws = Workspace(tmp_path / "run")
    ws.write("a.py", "one\n")
    first = ws.snapshot("a")
    ws.write("a.py", "two\n")
    second = ws.snapshot("b")
    assert first.fingerprint != second.fingerprint
