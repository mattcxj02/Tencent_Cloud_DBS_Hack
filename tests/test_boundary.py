"""AST boundary test: shared/ must not import gateway, ledger, agent or validator.

This is a static (import-time) check of invariant I1's shared-code clause.
Runtime isolation is tested separately in T-04.
"""

from __future__ import annotations

import ast
from pathlib import Path

# The shared/ package source root.
_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"

# Modules that shared/ must never import.
_FORBIDDEN_MODULES = {"gateway", "ledger", "agent", "validator"}


def _extract_imports(tree: ast.AST) -> set[str]:
    """Extract all top-level module names from import statements."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
        # Relative imports: node.level > 0 means intra-package.
        # These are fine (shared importing shared).
    return modules


class TestSharedPurity:
    """shared/ must be pure: no gateway/ledger/agent/validator imports."""

    def _all_python_files(self) -> list[Path]:
        return sorted(_SHARED_DIR.rglob("*.py"))

    def test_no_forbidden_imports(self) -> None:
        """No file in shared/ imports gateway, ledger, agent or validator."""
        files = self._all_python_files()
        assert len(files) > 0, "shared/ should have Python files"
        for f in files:
            source = f.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(f))
            imports = _extract_imports(tree)
            forbidden = imports & _FORBIDDEN_MODULES
            assert not forbidden, (
                f"{f.relative_to(_SHARED_DIR.parent)} imports forbidden modules: {forbidden}"
            )

    def test_no_io_imports(self) -> None:
        """No file in shared/ imports I/O modules (os, sys, sqlite3, etc.).

        ``re`` and ``json`` are allowed (pure stdlib).  ``os``, ``sys``,
        ``sqlite3``, ``socket``, ``subprocess``, ``http``, ``urllib``,
        ``pathlib`` (for file I/O), ``io``, ``logging`` are forbidden.
        """
        forbidden_io = {
            "os",
            "sys",
            "sqlite3",
            "socket",
            "subprocess",
            "http",
            "urllib",
            "io",
            "logging",
            "pathlib",
            "shutil",
            "tempfile",
            "dbm",
            "shelve",
            "pickle",
            "marshal",
            "asyncio",
            "multiprocessing",
            "signal",
        }
        files = self._all_python_files()
        for f in files:
            source = f.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(f))
            imports = _extract_imports(tree)
            found = imports & forbidden_io
            assert not found, f"{f.relative_to(_SHARED_DIR.parent)} imports I/O modules: {found}"

    def test_shared_imports_at_runtime(self) -> None:
        """Importing shared modules does not trigger forbidden imports."""
        import sys

        # Snapshot modules loaded before importing shared.
        before = set(sys.modules.keys())

        import shared.canonical  # noqa: F401
        import shared.lineage  # noqa: F401
        import shared.money  # noqa: F401
        import shared.schemas  # noqa: F401
        import shared.timeutil  # noqa: F401

        after = set(sys.modules.keys())
        newly_loaded = after - before
        for mod in _FORBIDDEN_MODULES:
            assert mod not in newly_loaded, f"importing shared/ pulled in forbidden module {mod!r}"
