"""Shared pytest fixtures."""

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from tests.t05_support import env  # noqa: F401  (fixture for the T-05 tests)

#: Langfuse settings with no keys: a Telemetry built from this is disabled (no export).
_NO_LANGFUSE = SimpleNamespace(
    langfuse_public_key=None, langfuse_secret_key=None, langfuse_base_url=None
)


@pytest.fixture(autouse=True)
def _gateway_telemetry_never_exports() -> Iterator[None]:
    """Every test starts with a disabled gateway telemetry facade, never one built from
    ``.env``, so no test can export to the real Langfuse project. Tests that check spans
    inject ``Telemetry.for_testing`` (in-memory exporter, unroutable URL)."""
    from gateway.telemetry import _set_gateway_telemetry
    from shared.telemetry import Telemetry

    _set_gateway_telemetry(Telemetry(_NO_LANGFUSE, "gateway"))
    yield
    _set_gateway_telemetry(Telemetry(_NO_LANGFUSE, "gateway"))
