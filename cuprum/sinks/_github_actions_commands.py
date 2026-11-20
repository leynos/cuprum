"""Workflow-command escaping and lease tokens for GitHub Actions sinks.

The runner parses these values as a wire format: data and property positions
have different escape rules, and the stop-commands token must be unguessable.
They live apart from the session that writes them so the wire vocabulary and
the framing lifecycle remain separate concerns.
"""

from __future__ import annotations

import secrets

_STOP_TOKEN_LENGTH = 16


def _escape_data(value: str) -> str:
    """Escape a value used in a workflow-command data position.

    GitHub Actions replaces ``%25``, ``%0D``, and ``%0A`` in the *data*
    segment after ``::command::``; see the workflow-command reference. The
    escaping here mirrors that contract.

    Returns
    -------
    str
        The escaped value safe for a workflow-command data position.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    """Escape a value used in a workflow-command property position.

    Property values (for example ``title=``) additionally escape ``:`` and
    ``,``, which delimit the property list.

    Returns
    -------
    str
        The escaped value safe for a workflow-command property position.
    """
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def _new_stop_token() -> str:
    """Return a fresh stop-commands token.

    The token must be unpredictable to the child whose output the lease
    shields: a guessable token would let hostile child output end the lease
    early and resume workflow-command processing. ``secrets.token_hex`` draws
    from the operating system's CSPRNG.

    Returns
    -------
    str
        A fresh unpredictable hexadecimal token.
    """
    return secrets.token_hex(_STOP_TOKEN_LENGTH // 2)


__all__ = [
    "_escape_data",
    "_escape_property",
    "_new_stop_token",
]
