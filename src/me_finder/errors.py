"""Shared error types that carry user-facing messages.

``UserFacingError`` marks an expected failure whose message is written for the
person using MEFinder. Controllers and the CLI turn it into an error response
or a printed message instead of treating it as a crash. Parser-specific errors
such as ``mineru_api.MinerUError`` subclass it, so catching ``UserFacingError``
also covers them.
"""

from __future__ import annotations


class UserFacingError(RuntimeError):
    """Expected failure whose message can be shown to the user as-is."""
