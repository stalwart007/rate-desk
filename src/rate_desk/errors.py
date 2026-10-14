"""Everything the service can refuse to do, and why.

Each error carries a stable ``code`` so the HTTP and MCP layers can map it
without parsing messages. The message is written for the person who caused it.
"""

from __future__ import annotations


class RateDeskError(Exception):
    code = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Unauthenticated(RateDeskError):
    code = "unauthenticated"


class NotFound(RateDeskError):
    code = "not_found"


class NotPermitted(RateDeskError):
    """The caller's role does not allow this action."""

    code = "not_permitted"


class FourEyesViolation(RateDeskError):
    """Somebody tried to decide a request they raised themselves."""

    code = "four_eyes"


class AlreadyDecided(RateDeskError):
    """Rule 3: a request is decided exactly once."""

    code = "already_decided"


class RateMismatch(RateDeskError):
    """The requester's "rate now" is not the rate actually in force."""

    code = "rate_mismatch"


class StaleRequest(RateDeskError):
    """The rate in force moved after this request was raised, so approving it
    would not do what the approver was shown."""

    code = "stale"


class InvalidRequest(RateDeskError):
    code = "invalid"
