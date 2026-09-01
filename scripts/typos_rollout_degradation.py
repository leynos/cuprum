"""Degradation telemetry and the HTTPS-only redirect policy.

This module owns the bounded refresh-degradation counters and the redirect
handler that refuses HTTPS-to-HTTP downgrades. It sits below
``typos_rollout_refresh``, which records degradations when it falls back to
cached data and installs the handler on its opener.
"""

from __future__ import annotations

import logging
import threading
import typing as typ
import urllib.error
import urllib.parse
import urllib.request

_logger = logging.getLogger(__name__)

# Bounded refresh-failure counter: one entry per known degradation reason, so
# the mapping cannot grow with attacker- or network-controlled input. URLs are
# deliberately never recorded — only the redirect target's scheme is.
_DEGRADATION_REASONS: typ.Final = (
    "https_redirect_downgrade",
    "stale_cache",
    "offline_cache",
)

# ``+=`` on a dict value is a read-modify-write, so concurrent refreshes could
# lose an increment. The counter is process-wide, so it is guarded by a
# process-wide lock rather than being made per-refresh.
_DEGRADATIONS_LOCK: typ.Final = threading.Lock()
_REFRESH_DEGRADATIONS: typ.Final[dict[str, int]] = dict.fromkeys(
    _DEGRADATION_REASONS, 0
)


def _record_degradation(reason: str) -> None:
    """Increment the bounded degradation counter for a known *reason*."""
    if reason not in _REFRESH_DEGRADATIONS:
        return
    with _DEGRADATIONS_LOCK:
        _REFRESH_DEGRADATIONS[reason] += 1


def reset_degradations() -> None:
    """Reset every degradation counter to zero.

    Exists so tests own the counter's starting state explicitly instead of
    asserting on deltas against whatever earlier tests left behind.
    """
    with _DEGRADATIONS_LOCK:
        _REFRESH_DEGRADATIONS.update(dict.fromkeys(_DEGRADATION_REASONS, 0))


def degradation_snapshot() -> dict[str, int]:
    """Return a consistent copy of the degradation counters.

    Returns
    -------
    dict[str, int]
        A snapshot mapping each degradation reason to its current count.
    """
    with _DEGRADATIONS_LOCK:
        return dict(_REFRESH_DEGRADATIONS)


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects that downgrade the shared source away from HTTPS.

    The refresh module's ``_https_request`` only constrains the *initial* URL.
    Without this handler the default opener would silently follow an
    ``https`` -> ``http`` redirect and fetch the dictionary in cleartext, so
    the downgrade is refused before urllib reissues the request.
    """

    @typ.override
    def redirect_request(
        self,
        req: urllib.request.Request,
        *args: object,
        **kwargs: object,
    ) -> urllib.request.Request | None:
        """Return the redirected request, refusing any non-HTTPS target.

        Returns
        -------
        urllib.request.Request | None
            The redirected request, or ``None`` when urllib declines to
            redirect.

        Raises
        ------
        urllib.error.URLError
            If the redirect target does not use HTTPS.
        """
        # The override's ``*args: object`` is broader than urllib's declared
        # ``HTTPRedirectHandler.redirect_request`` parameter types.
        redirected = super().redirect_request(  # pyright: ignore[reportArgumentType]
            req,
            *args,  # ty: ignore[invalid-argument-type] - override widens to *args: object
            **kwargs,
        )
        if redirected is None:
            return None
        scheme = urllib.parse.urlsplit(redirected.full_url).scheme
        if scheme != "https":
            _record_degradation("https_redirect_downgrade")
            _logger.warning(
                "Rejected shared dictionary redirect that left HTTPS",
                extra={
                    "event": "typos_rollout.https_redirect_downgrade",
                    "redirect_scheme": scheme,
                },
            )
            message = (
                f"shared dictionary redirect must stay on HTTPS, got scheme {scheme!r}"
            )
            raise urllib.error.URLError(message)
        return redirected
