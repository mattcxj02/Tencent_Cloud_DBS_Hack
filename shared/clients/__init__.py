"""Scoped HTTP clients for agent/validator to gateway communication.

These are read-only clients.  No execute/write methods exist here.
The only exception is POST ``/internal/bank/quotes`` for quote issue,
which creates a new (immutable) quote row.
"""
