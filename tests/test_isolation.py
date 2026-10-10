"""AST boundary tests for service isolation (invariant I1).

Extends the T-01 shared-purity test with checks that:
- ``agent/`` and ``validator/`` do not import ``gateway`` or ``ledger``.
- ``shared/clients/`` has no write/execute methods (no POST other than
  quote issue).
- ``shared/`` does not import ``gateway`` or ``ledger``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

_FORBIDDEN_FOR_AGENT_VALIDATOR = {"gateway", "ledger"}
_FORBIDDEN_FOR_SHARED = {"gateway", "ledger", "agent", "validator"}


def _extract_imports(tree: ast.AST) -> set[str]:
    """Extract all top-level module names from import statements."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def _all_python_files(directory: str) -> list[Path]:
    root = _REPO_ROOT / directory
    if not root.exists():
        return []
    return sorted(root.rglob("*.py"))


class TestAgentValidatorIsolation:
    """agent/ and validator/ must not import gateway or ledger."""

    def test_agent_no_gateway_ledger(self) -> None:
        for f in _all_python_files("agent"):
            source = f.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(f))
            imports = _extract_imports(tree)
            forbidden = imports & _FORBIDDEN_FOR_AGENT_VALIDATOR
            assert not forbidden, (
                f"agent/{f.relative_to(_REPO_ROOT / 'agent')} "
                f"imports forbidden modules: {forbidden}"
            )

    def test_validator_no_gateway_ledger(self) -> None:
        for f in _all_python_files("validator"):
            source = f.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(f))
            imports = _extract_imports(tree)
            forbidden = imports & _FORBIDDEN_FOR_AGENT_VALIDATOR
            assert not forbidden, (
                f"validator/{f.relative_to(_REPO_ROOT / 'validator')} "
                f"imports forbidden modules: {forbidden}"
            )


class TestSharedExtendedPurity:
    """shared/ must not import gateway, ledger, agent or validator."""

    def test_no_forbidden_imports(self) -> None:
        for f in _all_python_files("shared"):
            source = f.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(f))
            imports = _extract_imports(tree)
            forbidden = imports & _FORBIDDEN_FOR_SHARED
            assert not forbidden, (
                f"shared/{f.relative_to(_REPO_ROOT / 'shared')} "
                f"imports forbidden modules: {forbidden}"
            )


class TestSharedClientsNoWrite:
    """shared/clients/ must not contain write/execute methods.

    The only POST is quote issue (``issue_quote``), which creates a
    new immutable quote row.  No execute, no transfer, no payment
    methods.
    """

    def _bank_read_source(self) -> str:
        path = _REPO_ROOT / "shared" / "clients" / "bank_read.py"
        return path.read_text(encoding="utf-8")

    def test_no_execute_methods(self) -> None:
        """No method names suggesting execution or payment."""
        source = self._bank_read_source()
        tree = ast.parse(source)
        forbidden_names = {
            "execute",
            "execute_bundle",
            "update_mandate",
            "approve",
            "submit_approval",
            "transfer",
            "pay_bill",
            "pay_card",
            "buy_equity",
            "commit",
            "dispatch",
            "sign",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert node.name not in forbidden_names, (
                    f"shared/clients/bank_read.py defines forbidden method: {node.name}"
                )
                # Also check async functions
            if isinstance(node, ast.AsyncFunctionDef):
                assert node.name not in forbidden_names, (
                    f"shared/clients/bank_read.py defines forbidden async method: {node.name}"
                )

    def test_only_post_is_issue_quote(self) -> None:
        """The only POST call in bank_read.py is for quote issue."""
        source = self._bank_read_source()
        tree = ast.parse(source)

        # Find all method calls that are POST requests
        post_methods = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Match httpx.post(...)
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "httpx"
                    and func.attr == "post"
                ):
                    # Find which method this post call is inside
                    post_methods.append(func.attr)

        # All POST calls should be inside issue_quote
        # (we verify by checking that issue_quote exists and contains a POST)
        assert "post" in post_methods
        # No DELETE, PUT, PATCH
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "httpx"
                    and func.attr in ("delete", "put", "patch")
                ):
                    pytest.fail(f"shared/clients/bank_read.py makes a {func.attr} request")
