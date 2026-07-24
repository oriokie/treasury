"""Settling payables and accruals. See `payables` — this is its real name.

The module was written for payables and then generalised to accruals, which
settle identically. Both names resolve to the same functions so no caller had
to change; `obligations` is the one to reach for in new code.
"""
from .payables import (  # noqa: F401
    refresh_settlement, settle, unlink_payment)
