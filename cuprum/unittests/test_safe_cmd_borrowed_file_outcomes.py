"""The borrowed-*file-object* arm of the redirect matrix, on failure outcomes.

``test_safe_cmd_redirect.py`` owns the matrix — ``{owned path, borrowed fd,
borrowed file}`` crossed with ``{exit 0, non-zero exit, timeout, spawn
failure}`` — and says so in a comment above its cases. This module holds the two
cells of that matrix that will not fit there: the borrowed file object on a
timeout, and on the shared spawn-entry failure.

The split is a cohesion measure, not a subject change. The redirect suite sits
at its CodeScene cohesion ceiling once every case requests ``spawn_backend``,
as the matrix requires; adding another case to it drops the file to a ``Low
Cohesion`` finding, and that was measured rather than assumed. The subject is
unchanged, so these cases are written the way the ones in the redirect suite are
written, and the drift guard in ``test_safe_cmd_stdio_matrix_contract`` treats
all three modules as one matrix.

The borrowed half is the safety-critical half. "Cuprum closed a descriptor it
did not open" is invisible in the child's exit code and shows up only when the
caller next touches their own resource, so both cases *use* the borrowed object
after the failing run. A close cuprum did not owe raises from that write.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum.sh import SafeCmd, StdioTarget
from cuprum.unittests._stdio_redirect_support import (
    _NEVER_EXITS,
    _WRITE_STDOUT,
    _posix_only,
    _redirect_options,
    execution_strategy,
    python_builder,
    refusing_spawn,
    spawn_backend,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.unittests._stdio_redirect_support import ExecuteFn

# Fixtures are resolved by name from the test module's namespace, so the four
# that live in the support module are re-exported rather than merely imported.
# ``spawn_backend`` and ``refusing_spawn`` return nothing and are requested
# through ``@pytest.mark.usefixtures``, which resolves them by name too.
__all__ = [
    "execution_strategy",
    "python_builder",
    "refusing_spawn",
    "spawn_backend",
]


@_posix_only
@pytest.mark.usefixtures("spawn_backend")
def test_borrowed_file_object_survives_a_timeout(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """Teardown after a timeout leaves the borrowed file object usable.

    The descriptor arm of this cell lives in the redirect suite and proves
    cuprum does not close what it borrowed when the child is killed. This one
    proves the *object* survives the same teardown: the flush cuprum performs
    before spawning, and the ``fileno()`` it reads, must leave a handle the
    caller can keep writing through after the kill.

    Teardown after a timeout is the most aggressive path in the run — the child
    is killed, the writer is cancelled, and every library-owned resource is
    released — so it is the outcome most likely to lose the distinction between
    a resource cuprum owns and one it was lent.
    """
    _, execute = execution_strategy
    log = tmp_path / "borrowed.log"
    command = python_builder("-c", _NEVER_EXITS)

    with log.open("w+b") as handle:
        with pytest.raises(TimeoutError):
            execute(
                command,
                {
                    "output": _redirect_options(stdout=StdioTarget.fd(handle)),
                    "timeout": 0.05,
                },
            )

        handle.write(b"caller\n")
        handle.flush()

    written = log.read_bytes()
    assert written == b"caller\n", (
        "a timed-out child wrote nothing, and the caller's own write must "
        f"still land through the object cuprum was lent; got {written!r}"
    )


@_posix_only
@pytest.mark.usefixtures("refusing_spawn")
def test_borrowed_file_object_survives_a_spawn_failure(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
    tmp_path: Path,
) -> None:
    """A spawn that never happened leaves the borrowed file object usable.

    The borrowed flush runs between opening the owned targets and the spawn
    call, so a spawn failure lands after cuprum has already touched the
    borrowed object. The failure unwinds through the ``finally`` that closes
    the *owned* descriptors, which is precisely where a borrowed one could be
    swept up with them.

    This is the *shared* spawn-entry failure — the dispatch refusing before any
    backend was chosen — so it is the counterpart of the borrowed-descriptor
    case in the redirect suite, not a duplicate of the backend-specific arm in
    ``test_safe_cmd_spawn_failure_backends``.
    """
    _, execute = execution_strategy
    log = tmp_path / "borrowed.log"
    command = python_builder("-c", _WRITE_STDOUT)

    with log.open("w+b") as handle:
        with pytest.raises(FileNotFoundError):
            execute(
                command, {"output": _redirect_options(stdout=StdioTarget.fd(handle))}
            )

        handle.write(b"caller\n")
        handle.flush()

    written = log.read_bytes()
    assert written == b"caller\n", (
        "a child that never started wrote nothing, and the caller's own write "
        f"must still land through the object cuprum was lent; got {written!r}"
    )
