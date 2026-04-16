from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from owl import MiniAgent
from owl.workspace import WorkspaceContext


_TEST_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "test-runtime"
_TEST_TEMP_ROOT = _TEST_RUNTIME_ROOT / "temp"
_TEST_PYTEST_ROOT = _TEST_RUNTIME_ROOT / "pytest"
_ORIGINAL_MKDTEMP = tempfile.mkdtemp
_ORIGINAL_TEMP_DIR_CLASS = tempfile.TemporaryDirectory
_ORIGINAL_WORKSPACE_BUILD = WorkspaceContext.build.__func__


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reset_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(raw: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw)).strip("-")
    return text[:80] or "tmp"


class LocalTmpPathFactory:
    def __init__(self, root: Path) -> None:
        self._root = _ensure_dir(root)
        self._counter = 0

    def getbasetemp(self) -> Path:
        return self._root

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        base = _safe_name(basename)
        if not numbered:
            return _reset_dir(self._root / base)
        self._counter += 1
        return _reset_dir(self._root / f"{base}-{self._counter}")


def _local_mkdtemp(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
    root = Path(dir) if dir else _TEST_TEMP_ROOT
    _ensure_dir(root)
    name = f"{prefix or 'tmp'}{uuid.uuid4().hex[:8]}{suffix or ''}"
    path = root / name
    path.mkdir(parents=True, exist_ok=False)
    return str(path)


class _LocalTemporaryDirectory:
    def __init__(self, suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> None:
        self.name = _local_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        with suppress(Exception):
            shutil.rmtree(self.name, ignore_errors=True)


def _inside_test_runtime(path: Path) -> bool:
    path = path.resolve()
    return path == _TEST_RUNTIME_ROOT or _TEST_RUNTIME_ROOT in path.parents


def _workspace_build_for_tests(cls, cwd, repo_root_override=None, include_git_metadata=True):
    cwd_path = Path(cwd).resolve()
    if repo_root_override is None and _inside_test_runtime(cwd_path):
        return _ORIGINAL_WORKSPACE_BUILD(
            cls,
            cwd_path,
            repo_root_override=str(cwd_path),
            include_git_metadata=False,
        )
    return _ORIGINAL_WORKSPACE_BUILD(
        cls,
        cwd_path,
        repo_root_override=repo_root_override,
        include_git_metadata=include_git_metadata,
    )


def pytest_configure(config: pytest.Config) -> None:
    _ensure_dir(_TEST_RUNTIME_ROOT)
    _ensure_dir(_TEST_TEMP_ROOT)
    _ensure_dir(_TEST_PYTEST_ROOT)
    os.environ["TEMP"] = str(_TEST_TEMP_ROOT)
    os.environ["TMP"] = str(_TEST_TEMP_ROOT)
    os.environ["TMPDIR"] = str(_TEST_TEMP_ROOT)
    tempfile.tempdir = str(_TEST_TEMP_ROOT)
    tempfile.mkdtemp = _local_mkdtemp
    tempfile.TemporaryDirectory = _LocalTemporaryDirectory
    WorkspaceContext.build = classmethod(_workspace_build_for_tests)
    # Force pytest internals to stay inside the repo instead of system temp.
    config.option.basetemp = str(_TEST_PYTEST_ROOT / f"session-{os.getpid()}")


def pytest_unconfigure(config: pytest.Config) -> None:
    tempfile.mkdtemp = _ORIGINAL_MKDTEMP
    tempfile.TemporaryDirectory = _ORIGINAL_TEMP_DIR_CLASS
    WorkspaceContext.build = classmethod(_ORIGINAL_WORKSPACE_BUILD)


@pytest.fixture(scope="session")
def tmp_path_factory() -> LocalTmpPathFactory:
    root = _TEST_RUNTIME_ROOT / "tmp-path-factory"
    _ensure_dir(root)
    return LocalTmpPathFactory(root)


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest, tmp_path_factory: LocalTmpPathFactory) -> Path:
    node_name = request.node.name or request.node.nodeid
    unique = f"{_safe_name(node_name)}-{uuid.uuid4().hex[:8]}"
    return tmp_path_factory.mktemp(unique, numbered=False)


@pytest.fixture(autouse=True)
def isolate_semantic_db(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_root = _ensure_dir(_TEST_RUNTIME_ROOT / "semantic-db")
    db_path = db_root / f"{_safe_name(request.node.nodeid)}.db"
    with suppress(Exception):
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        with suppress(Exception):
            Path(str(db_path) + suffix).unlink()

    def _semantic_memory_db_path(self: MiniAgent) -> str:
        return str(db_path)

    monkeypatch.setattr(MiniAgent, "semantic_memory_db_path", _semantic_memory_db_path, raising=True)
    return db_path
