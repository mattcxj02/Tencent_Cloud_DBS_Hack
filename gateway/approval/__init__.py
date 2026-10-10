"""Approval package — eligibility hooks and state-machine entry points.

``hooks`` wires the real policy pre-check and atomic commit (T-06); semantic
validation stays fail-closed until T-10.  Tests override hooks via
:func:`gateway.approval.hooks._set_hook`, which is not reachable from HTTP.
"""
