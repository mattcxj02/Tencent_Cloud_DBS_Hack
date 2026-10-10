"""The gateway's single source of "now" for approval, policy and execution.

Every time-dependent gateway decision (envelope issue/expiry, quote expiry,
KYC/mandate expiry, Asia/Singapore daily windows, the rolling velocity window)
reads :func:`utcnow`.  Pure functions such as ``policy.evaluate`` and
``execution.commit`` take ``now`` as an explicit argument instead.

Tests swap the clock with :func:`_set_clock`.  That is a module-level test
hook: there is no header, query parameter or environment variable that can
change the clock of a running gateway.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from shared.timeutil import now_utc, validate_aware

__all__ = ["utcnow"]

_clock: Callable[[], datetime] | None = None


def utcnow() -> datetime:
    """Current UTC time (timezone-aware), from the injected clock if one is set."""
    return validate_aware(_clock()) if _clock is not None else now_utc()


def _set_clock(clock: Callable[[], datetime] | None) -> None:
    """Test hook: replace the clock, or restore the system clock with ``None``."""
    global _clock
    _clock = clock
