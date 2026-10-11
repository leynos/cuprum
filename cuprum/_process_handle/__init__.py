"""The owned handle behind ``SafeCmd.start()``.

One running command, owned end to end: spawned, announced, waited on, signalled,
stopped, and settled. The package is split so each question lives in one place.

- :mod:`cuprum._process_handle.state` holds :class:`RunningCommand`, the public
  handle itself: what a caller can ask of a running child, and what each
  answer guarantees.
- :mod:`cuprum._process_handle.settlement` states the one-settlement rule every
  operation goes through, and the record it leaves behind.
- :mod:`cuprum._process_handle.tasks` owns the record a run is held by and the
  spawn and task wiring that builds it.
- :mod:`cuprum._process_handle.control` delivers signals and escalates from
  ``SIGTERM`` to ``SIGKILL``.
- :mod:`cuprum._process_handle.coordinator` waits, reconciles the run's tasks,
  assembles the ``CommandResult``, and emits the exit event.

The two signal-delivery failures the handle can raise live outside the package,
in :mod:`cuprum._process_handle_errors`, so the ``cuprum.sh`` facade can
re-export them without importing the handle's internals.
"""

from cuprum._process_handle.state import RunningCommand as RunningCommand

__all__ = ["RunningCommand"]
