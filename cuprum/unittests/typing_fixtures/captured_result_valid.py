"""Static-check fixture for capture-sensitive command result types.

This module is checked by ``ty`` but is never imported or executed. It pins the
text-stream guarantee on default single-command runs while preserving the
optional stream type for explicit output options.
"""

from __future__ import annotations

import typing as typ

from cuprum import ECHO, CapturedResult, CommandResult, RunOutputOptions, sh


def default_sync_run_returns_captured_streams() -> None:
    """Assert that the default synchronous call exposes decoded text streams."""
    command = sh.make(ECHO)()
    result = command.run_sync()
    typ.assert_type(result, CapturedResult)
    typ.assert_type(result.stdout, str)
    typ.assert_type(result.stderr, str)


async def default_async_run_returns_captured_streams() -> None:
    """Assert that awaiting the default call exposes decoded text streams."""
    command = sh.make(ECHO)()
    result = await command.run()
    typ.assert_type(result, CapturedResult)
    typ.assert_type(result.stdout, str)
    typ.assert_type(result.stderr, str)


def explicit_none_sync_run_returns_captured_streams() -> None:
    """Preserve the default capture overload with explicit ``None``."""
    command = sh.make(ECHO)()
    result = command.run_sync(output=None)
    typ.assert_type(result, CapturedResult)
    typ.assert_type(result.stdout, str)
    typ.assert_type(result.stderr, str)


async def explicit_none_async_run_returns_captured_streams() -> None:
    """Preserve the capture overload when awaiting with ``output=None``."""
    command = sh.make(ECHO)()
    result = await command.run(output=None)
    typ.assert_type(result, CapturedResult)
    typ.assert_type(result.stdout, str)
    typ.assert_type(result.stderr, str)


def explicit_sync_options_keep_optional_streams() -> None:
    """Even explicit capture settings retain the broader result contract."""
    command = sh.make(ECHO)()
    result = command.run_sync(output=RunOutputOptions(capture=True))
    typ.assert_type(result, CommandResult)
    typ.assert_type(result.stdout, str | None)
    typ.assert_type(result.stderr, str | None)


async def explicit_async_options_keep_optional_streams() -> None:
    """Explicit options keep the async return type broad as well."""
    command = sh.make(ECHO)()
    result = await command.run(output=RunOutputOptions(capture=True))
    typ.assert_type(result, CommandResult)
    typ.assert_type(result.stdout, str | None)
    typ.assert_type(result.stderr, str | None)


def optional_sync_options_keep_the_broader_return_type(
    output: RunOutputOptions | None,
) -> None:
    """Preserve the broader type when options may disable capture."""
    command = sh.make(ECHO)()
    result = command.run_sync(output=output)
    typ.assert_type(result, CommandResult)
    typ.assert_type(result.stdout, str | None)
    typ.assert_type(result.stderr, str | None)


async def optional_async_options_keep_the_broader_return_type(
    output: RunOutputOptions | None,
) -> None:
    """Accept optional-options variables in the async overload."""
    command = sh.make(ECHO)()
    result = await command.run(output=output)
    typ.assert_type(result, CommandResult)
    typ.assert_type(result.stdout, str | None)
    typ.assert_type(result.stderr, str | None)


def require_captured_narrows_explicit_options() -> None:
    """Check capture at the point a caller needs non-optional streams."""
    command = sh.make(ECHO)()
    result = command.run_sync(output=RunOutputOptions())
    captured = result.require_captured()
    typ.assert_type(captured, CapturedResult)
    typ.assert_type(captured.stdout, str)
    typ.assert_type(captured.stderr, str)


def captured_results_remain_command_results() -> None:
    """Accept captured results as existing command results."""
    command = sh.make(ECHO)()
    captured = command.run_sync()
    _accepts_command_result(captured)


def _accepts_command_result(result: CommandResult) -> None:
    """Accept the existing base type to prove subclass assignability."""
    typ.assert_type(result, CommandResult)
