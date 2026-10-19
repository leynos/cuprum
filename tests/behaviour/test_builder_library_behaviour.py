"""Behavioural tests for the core builder library.

The step implementations live in ``tests.behaviour._builder_library_steps`` so
this module stays within the 400-line limit. pytest-bdd resolves a step against
the fixtures visible to the module that declares the scenario, and a plain
``import`` does not re-export a fixture, so every step is bound here to the
support module's plain implementation. The decorators live only in this module,
so each step text and ``target_fixture`` name has a single source of truth.
"""

from __future__ import annotations

from pytest_bdd import given, parsers, scenario, then, when

from tests.behaviour import _builder_library_steps as _steps

given("the core git builders")(_steps.given_core_git_builders)
given("valid rsync paths", target_fixture="rsync_paths")(_steps.given_valid_rsync_paths)
given("a tar archive and source paths", target_fixture="tar_inputs")(
    _steps.given_tar_inputs
)
given(
    "a relative tar source beginning with --checkpoint",
    target_fixture="relative_tar_inputs",
)(_steps.given_relative_tar_inputs)
given(
    "relative rsync paths beginning with --",
    target_fixture="relative_rsync_paths",
)(_steps.given_relative_rsync_paths)
when(
    parsers.parse('I build a git checkout command for ref "{ref}"'),
    target_fixture="git_result",
)(_steps.when_build_git_checkout)
when(
    "I build an rsync sync command with archive enabled",
    target_fixture="rsync_result",
)(_steps.when_build_rsync_command)
when(
    "I build a tar create command with gzip enabled",
    target_fixture="tar_result",
)(_steps.when_build_tar_command)
when(
    "I build a relative tar create command",
    target_fixture="relative_tar_result",
)(_steps.when_build_relative_tar_command)
when("I build a relative rsync sync command", target_fixture="relative_rsync_result")(
    _steps.when_build_relative_rsync_command
)
when(
    parsers.parse(
        'I build an rsync sync command with a remote "{role}" operand "{operand}"',
    ),
    target_fixture="rsync_result",
)(_steps.when_build_rsync_command_with_remote_operand)
then(parsers.parse('the git command argv is "{program}" "{verb}" "{ref}"'))(
    _steps.then_git_argv_matches
)
then("a git ref validation error is raised")(_steps.then_git_ref_error_raised)
then(parsers.parse('the rsync command argv includes "{flag}" and the paths'))(
    _steps.then_rsync_argv_matches
)
then(parsers.parse('the tar command argv includes "{flag}" and the paths'))(
    _steps.then_tar_argv_matches
)
then("the tar sources follow the end-of-options separator")(
    _steps.then_tar_sources_follow_separator
)
then("the rsync operands follow the end-of-options separator")(
    _steps.then_rsync_operands_follow_separator
)
then(
    parsers.parse('an rsync operand validation error is raised for the "{role}"'),
)(_steps.then_rsync_operand_error_raised)


@scenario(
    "../features/builder_library.feature",
    "Git checkout builder constructs argv",
)
def test_git_checkout_builder() -> None:
    """Behavioural coverage for git checkout builder."""


@scenario(
    "../features/builder_library.feature",
    "Invalid git ref is rejected",
)
def test_git_ref_rejection() -> None:
    """Behavioural coverage for git ref validation."""


@scenario(
    "../features/builder_library.feature",
    "Rsync builder constructs argv with safe paths",
)
def test_rsync_builder() -> None:
    """Behavioural coverage for rsync builder."""


@scenario(
    "../features/builder_library.feature",
    "Tar create builder constructs argv",
)
def test_tar_builder() -> None:
    """Behavioural coverage for tar builder."""


@scenario(
    "../features/builder_library.feature",
    "Rsync builder rejects remote operands",
)
def test_rsync_remote_operand_rejection() -> None:
    """Behavioural coverage for the rsync local-only operand policy."""


@scenario(
    "../features/builder_library.feature",
    "Tar create separates option-shaped relative sources",
)
def test_tar_builder_separates_option_shaped_sources() -> None:
    """Behavioural coverage for the tar end-of-options separator."""


@scenario(
    "../features/builder_library.feature",
    "Rsync separates option-shaped relative operands",
)
def test_rsync_builder_separates_option_shaped_operands() -> None:
    """Behavioural coverage for the rsync end-of-options separator."""
