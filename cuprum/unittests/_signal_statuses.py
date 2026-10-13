"""The exit statuses the handle's process doubles report for a stop.

A graceful stop leaves behind the status ``SIGTERM`` produces, and a forced one
the status ``SIGKILL`` produces. Both are the doubles' model of what a real
child's ``wait()`` would return, and the handle's tests assert against them:
that model is what makes a graceful stop distinguishable from an escalation, so
a handle that escalated without being asked reports ``-9`` where the case
expected ``-15`` and the assertion sees it.

The statuses live apart from the doubles that use them because of when they are
resolved. Deciding them needs ``signal.SIGKILL``, which CPython does not define
on Windows: there a forced stop is ``TerminateProcess`` and carries no signal
status at all, so there is nothing to assert rather than merely nothing to
import. Resolving them in the same module as the doubles would put an
``AttributeError`` on the import path of every test module that drives one,
ahead of any guard those modules could place.

So the guard sits here, above the only POSIX evaluation, and a Windows run
skips at import instead of raising. That skip then propagates: a module which
imports a support module skipped at module level is itself reported skipped, so
this one guard retires the whole POSIX-specific family rather than leaving each
consumer to repeat it.

Falling back to invented numbers was the alternative and is worse. A Windows
run reporting ``-9`` for a stop that has no status would be asserting a premise
the platform does not implement, and passing on it.
"""

from __future__ import annotations

import signal

import pytest

if not hasattr(signal, "SIGKILL"):
    pytest.skip(
        "a forced stop carries no signal status on this platform",
        allow_module_level=True,
    )

#: Status a graceful stop leaves behind, and the one an escalation leaves.
_TERMINATED = -signal.SIGTERM
_KILLED = -signal.SIGKILL
