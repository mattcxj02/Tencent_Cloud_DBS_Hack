"""UTC timestamp utilities.

All signed timestamps use one UTC ISO-8601 format with a ``Z`` suffix at
**millisecond** precision:

    ``YYYY-MM-DDTHH:MM:SS.mmmZ``

Millisecond precision is chosen because the 120-second approval window
makes sub-second ordering relevant, and sub-millisecond precision is not
meaningful in a prototype backed by SQLite.

Naive datetimes (without ``tzinfo``) are rejected.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

__all__ = [
    "now_utc",
    "to_iso",
    "from_iso",
    "validate_aware",
]

_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# Match ISO-8601 with Z suffix, milliseconds required.
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def validate_aware(dt: datetime) -> datetime:
    """Return ``dt`` if it is timezone-aware in UTC, else raise."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected; must be UTC-aware")
    if dt.tzinfo != UTC:
        # Convert to UTC.
        dt = dt.astimezone(UTC)
    return dt


def now_utc() -> datetime:
    """Current UTC time as a timezone-aware datetime."""
    return datetime.now(tz=UTC)


def to_iso(dt: datetime) -> str:
    """Format a datetime as ``YYYY-MM-DDTHH:MM:SS.mmmZ``.

    Naive datetimes are rejected.  The datetime must be UTC.
    """
    dt = validate_aware(dt)
    # Truncate to milliseconds (3 digits), not microseconds (6).
    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"


def from_iso(s: str) -> datetime:
    """Parse an ISO-8601 ``Z`` string into a UTC-aware datetime.

    Only the exact millisecond format is accepted.
    """
    if not _ISO_RE.match(s):
        raise ValueError(f"timestamp must be YYYY-MM-DDTHH:MM:SS.mmmZ; got {s!r}")
    # Python's fromisoformat handles the Z suffix in 3.12+.
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
