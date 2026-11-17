"""Tests for the rsync local-only operand policy.

``rsync_sync`` accepts local operands only. These tests pin the two rejection
layers separately: the generic host-native ``safe_path`` policy, and the rsync
grammar check that runs after it. Fixtures are chosen so that each assertion
fails for the layer it names, on both POSIX and Windows.

The parameterized examples state the named spellings from the ticket. The
property tests beside them generalize the same invariant over generated
``host:path`` and ``host::module/path`` operands, and include a non-vacuity
property so the rejection cannot pass for the wrong reason.

Option separation and endpoint validation are complementary, and both are
pinned here. A local name shaped like rsync's remote-shell option (``-e``,
``--rsh=cmd``) stays a path operand only because ``--`` ends option parsing;
a generated ``.//--rsh=x`` property covers normalization as well. A remote
partner in the opposite role must still be rejected before ``sh.make`` runs.
"""

from __future__ import annotations

import sys
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import sh
from cuprum.builders import RsyncOptions, rsync_sync, safe_path
from cuprum.builders.rsync import _local_rsync_operand, _rsync_argv

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="host-native absolute semantics differ on Windows"
)
# Each spelling is a remote operand for rsync even though safe_path accepts it
# once relative paths are enabled. The drive-prefixed rows are separate because
# only one layer rejects them on each host.
_REMOTE_OPERANDS = (
    "host:path",
    "host:/path",
    "host::module/path",
    "host::module",
    "user@host:path",
    "rsync://host/module/dir",
    "RSYNC://host/module/dir",
    "[::1]:path",
    "./host:path",
)
_DRIVE_OPERANDS = ("C:/path", "C:\\path")
# A local operand accepted on every host. A native absolute path is rejected on
# the host whose grammar does not treat it as absolute, and a Windows drive
# path is remote-ending syntax for rsync everywhere. The MSYS mount spelling
# keeps its colon after a separator, so rsync reads it as local, and
# ``allow_relative=True`` accepts it on POSIX and Windows alike.
_MSYS_LOCAL = "/cygdrive/c/data"
_ROLES = ("source", "destination")
_RELATIVE = RsyncOptions(allow_relative=True)
# Rsync options that choose the remote-shell program. Reaching rsync in option
# position would run the named command with the caller's privileges; after
# ``--`` the same spelling is an ordinary local file name.
_REMOTE_SHELL_OPERANDS = ("-e", "-esh", "--rsh", "--rsh=cmd", "-e=cmd")
# Remote partners for the opposite operand, one per spelling family that the
# rejection hint must name.
_REMOTE_PARTNERS = ("host:path", "rsync://host/mod")
# A local relative partner accepted on every host.
_LOCAL_RELATIVE = "data/local"
# A local absolute partner accepted on every host: ``PurePath`` calls this UNC
# anchor absolute on POSIX and Windows alike, its first character is a
# separator, and it carries no colon. The portable fixture in
# ``test_builder_library.py`` documents the same spelling.
_PORTABLE_ABSOLUTE = "//rsync-scenario/share/data"
# The fragments the role-specific rejection must name: the supported remote
# spellings, so a caller learns what rsync would have read as an endpoint.
_REMOTE_HINT_FRAGMENTS = ("host:path", "host::module", "rsync://")
_ALL_FLAGS = ("--archive", "--delete", "--dry-run", "--verbose", "--compress")
_ALL_FLAG_OPTIONS = RsyncOptions(
    archive=True,
    delete=True,
    dry_run=True,
    verbose=True,
    compress=True,
    allow_relative=True,
)


def _operands(role: str, operand: str, local: str) -> tuple[str, str]:
    """Return ``(source, destination)`` with ``operand`` in ``role``'s place."""
    return (operand, local) if role == "source" else (local, operand)


def _assert_remote_rejection(message: str, role: str) -> None:
    """Assert a rejection names its role and every supported remote spelling."""
    assert f"rsync {role} must be a local path" in message, (
        f"the rejection must name the {role} operand; got {message!r}"
    )
    for fragment in _REMOTE_HINT_FRAGMENTS:
        assert fragment in message, (
            f"the rejection must name the {fragment!r} spelling; got {message!r}"
        )


def _record_factory_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace ``sh.make`` with a recorder and return the recorded calls.

    The caller owns the returned list so it can be inspected after the call
    under test. Recording the factory call is what establishes that rejection
    precedes construction: a builder that built a command and then raised would
    still satisfy an assertion that only checks no command was returned.

    Returns
    -------
    list[object]
        The list that each factory call appends its program to.
    """
    factory_calls: list[object] = []

    def record_factory_call(program: object) -> cabc.Callable[..., object]:
        """Record that the command factory ran, and return an unusable builder."""
        factory_calls.append(program)

        def refuse(*_argv: str) -> object:
            """Stop the builder if the factory is ever reached."""
            pytest.fail("sh.make must not run for a rejected operand")

        return refuse

    monkeypatch.setattr(sh, "make", record_factory_call)
    return factory_calls


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _REMOTE_OPERANDS)
def test_rsync_rejects_remote_operands_when_relative(
    operand: str,
    role: str,
) -> None:
    """With relative paths enabled, remote syntax is rejected by name.

    The message must also list the remote spellings the policy understands,
    so a caller can tell a rejected endpoint from a rejected local name.
    """
    with pytest.raises(
        ValueError, match=rf"rsync {role} must be a local path"
    ) as exc_info:
        rsync_sync(*_operands(role, operand, _MSYS_LOCAL), options=_RELATIVE)
    _assert_remote_rejection(str(exc_info.value), role)


@_POSIX_ONLY
@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _REMOTE_OPERANDS + _DRIVE_OPERANDS)
def test_rsync_rejects_remote_operands_by_default(
    operand: str,
    role: str,
    tmp_path: Path,
) -> None:
    """Default options reject every remote spelling, whatever layer reports it.

    Posix-only: the neutral operand is a native absolute path, which on Windows
    would itself carry a drive prefix and be read as remote.
    """
    source, destination = _operands(role, operand, tmp_path.as_posix())
    with pytest.raises(
        ValueError, match=rf"SafePath|rsync {role} must be a local path"
    ):
        rsync_sync(source, destination)


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _DRIVE_OPERANDS)
def test_rsync_rejects_drive_prefixed_operands_when_relative(
    operand: str,
    role: str,
) -> None:
    """A drive prefix stays a remote operand even with relative paths allowed."""
    with pytest.raises(ValueError, match=rf"rsync {role} must be a local path"):
        rsync_sync(*_operands(role, operand, _MSYS_LOCAL), options=_RELATIVE)


@pytest.mark.parametrize("operand", ["dir/file", "dir/host:path", _MSYS_LOCAL])
def test_rsync_accepts_portable_local_operands_unchanged(operand: str) -> None:
    """Accepted local operands reach the argv as safe_path normalizes them."""
    expected = str(safe_path(operand, allow_relative=True))
    cmd = rsync_sync(operand, operand, options=_RELATIVE)
    assert cmd.argv == ("--", expected, expected), (
        "accepted local operands must follow -- in normalized form"
    )


@_POSIX_ONLY
@pytest.mark.parametrize("suffix", ["", "/host:path"])
def test_rsync_accepts_native_absolute_operands_by_default(
    suffix: str,
    tmp_path: Path,
) -> None:
    """Native absolute operands, including one with a later colon, stay local."""
    operand = tmp_path.as_posix() + suffix
    expected = str(safe_path(operand))
    cmd = rsync_sync(operand, operand)
    assert cmd.argv == ("--", expected, expected), (
        "native absolute operands must follow -- in normalized form"
    )


@pytest.mark.parametrize("operand", ["host:path", "host::module", "host::module/path"])
def test_rsync_local_operand_rejects_remote_syntax_directly(operand: str) -> None:
    """The private operand check rejects remote syntax independent of _rsync_argv."""
    with pytest.raises(
        ValueError, match="rsync source must be a local path"
    ) as exc_info:
        _local_rsync_operand(operand, role="source", allow_relative=True)
    _assert_remote_rejection(str(exc_info.value), "source")


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _REMOTE_OPERANDS)
def test_rsync_rejection_precedes_command_construction(
    operand: str,
    role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected operand never reaches ``sh.make``, in either role.

    Asserting that no command was returned is weaker than it looks: the builder
    could construct one and then raise, discarding it. Recording the factory
    call itself is what establishes the ordering the ExecPlan requires.
    """
    factory_calls = _record_factory_calls(monkeypatch)
    with pytest.raises(ValueError, match=rf"rsync {role} must be a local path"):
        rsync_sync(*_operands(role, operand, _MSYS_LOCAL), options=_RELATIVE)
    assert not factory_calls, "the command factory must not be reached"


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _REMOTE_SHELL_OPERANDS)
def test_rsync_remote_shell_shaped_operands_stay_after_the_separator(
    operand: str,
    role: str,
) -> None:
    """A dash-prefixed local name cannot select rsync's remote shell.

    ``--`` ends option parsing, so a name spelled like ``--rsh=cmd`` is copied
    as a file rather than executed as the program that moves the bytes.
    """
    source, destination = _operands(role, operand, _LOCAL_RELATIVE)
    expected_source = str(safe_path(source, allow_relative=True))
    expected_destination = str(safe_path(destination, allow_relative=True))
    cmd = rsync_sync(source, destination, options=_ALL_FLAG_OPTIONS)

    assert cmd.argv.count("--") == 1, (
        "exactly one end-of-options separator must be emitted"
    )
    assert cmd.argv == (*_ALL_FLAGS, "--", expected_source, expected_destination), (
        "the separator must follow every flag, then the operands in role order"
    )
    separator = cmd.argv.index("--")
    assert cmd.argv[separator + 1 :] == (expected_source, expected_destination), (
        "the source and destination must be the final two elements in order"
    )
    assert all(not element.startswith(operand) for element in cmd.argv[:separator]), (
        "no option-position element may carry the operand"
    )


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _REMOTE_SHELL_OPERANDS)
@pytest.mark.parametrize("partner", _REMOTE_PARTNERS)
def test_rsync_remote_shell_operand_with_remote_partner_is_rejected_before_construction(
    operand: str,
    role: str,
    partner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An option-shaped local name cannot ride in with a remote endpoint.

    The option-shaped name sits in the opposite role, so a builder that
    validated only that operand would let the remote partner through. The
    remote operand is rejected before ``sh.make`` runs, whichever role holds it.
    """
    factory_calls = _record_factory_calls(monkeypatch)
    source, destination = _operands(role, partner, operand)
    with pytest.raises(
        ValueError, match=rf"rsync {role} must be a local path"
    ) as exc_info:
        rsync_sync(source, destination, options=_RELATIVE)
    _assert_remote_rejection(str(exc_info.value), role)
    assert not factory_calls, "the command factory must not be reached"


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _REMOTE_SHELL_OPERANDS)
def test_rsync_rejects_remote_shell_shaped_operands_by_default(
    operand: str,
    role: str,
) -> None:
    """Without the relative opt-in, the generic policy rejects the name first."""
    source, destination = _operands(role, operand, _PORTABLE_ABSOLUTE)
    with pytest.raises(ValueError, match="SafePath requires an absolute path"):
        rsync_sync(source, destination)


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("operand", _REMOTE_OPERANDS + _DRIVE_OPERANDS)
def test_rsync_rejects_remote_and_drive_operands_by_default_on_every_host(
    operand: str,
    role: str,
) -> None:
    """Default options reject every remote or drive spelling on both hosts.

    Unlike the POSIX-only matrix above, the neutral partner is the portable UNC
    anchor, which ``PurePath`` calls absolute on POSIX and Windows alike. The
    destination role therefore reaches the remote operand on both hosts, so
    each rejection is attributable to the spelling rather than to the partner.
    """
    source, destination = _operands(role, operand, _PORTABLE_ABSOLUTE)
    with pytest.raises(
        ValueError, match=rf"SafePath|rsync {role} must be a local path"
    ) as exc_info:
        rsync_sync(source, destination)
    message = str(exc_info.value)
    assert "SafePath" in message or f"rsync {role} must be a local path" in message, (
        f"the {role} operand must be rejected by one of the two policy layers; "
        f"got {message!r}"
    )


# Path segments avoid ".", NUL, and separators so generated paths are always
# SafePath-valid and normalization is the identity.
_GENERATED_SEGMENTS = st.text(alphabet="abcxyz09-_", min_size=1, max_size=6)
_GENERATED_ABSOLUTE_PATHS = st.lists(_GENERATED_SEGMENTS, min_size=1, max_size=4).map(
    lambda parts: "/" + "/".join(parts)
)
# A host segment that cannot itself contain the separator the strategy adds.
_GENERATED_HOSTS = st.text(alphabet="abcxyz09-_", min_size=1, max_size=8)
# Remote operands are accepted by safe_path(allow_relative=True); only the
# rsync-local check can reject them, so every generated example reaches it.
_GENERATED_REMOTE_OPERANDS = st.builds(
    lambda host, separator, path: f"{host}{separator}{path}",
    host=_GENERATED_HOSTS,
    separator=st.sampled_from([":", "::"]),
    path=st.lists(_GENERATED_SEGMENTS, min_size=1, max_size=3).map("/".join),
)
# The opposite operand in each rejection property must be a valid local path
# on every host. The MSYS mount spelling keeps its colon after a separator, so
# rsync reads it as local on either host.
_LOCAL_OPERAND = _MSYS_LOCAL


def _has_remote_spelling(operand: str) -> bool:
    """Report whether ``operand`` uses rsync's remote operand grammar."""
    if operand.lower().startswith("rsync://"):
        return True
    colon = operand.find(":")
    slash = operand.find("/")
    return colon != -1 and (slash == -1 or colon < slash)


def _emitted_operand(argv: tuple[str, ...], role: str) -> str:
    """Return the source or destination operand from a built rsync argv."""
    return argv[-1] if role == "destination" else argv[-2]


@settings(max_examples=200)
@given(operand=_GENERATED_REMOTE_OPERANDS, is_source=st.booleans())
def test_rsync_rejects_remote_specification_in_either_operand(
    operand: str,
    *,
    is_source: bool,
) -> None:
    """Generated host-colon-path operands are rejected as source and destination."""
    local = _LOCAL_OPERAND
    source, destination = (operand, local) if is_source else (local, operand)
    role = "source" if is_source else "destination"
    with pytest.raises(ValueError, match=rf"rsync {role} must be a local path"):
        _rsync_argv(source, destination, RsyncOptions(allow_relative=True))


@settings(max_examples=200)
@given(candidate=_GENERATED_REMOTE_OPERANDS | _GENERATED_ABSOLUTE_PATHS)
def test_rsync_accepted_operands_are_never_remote(candidate: str) -> None:
    """Every operand the builder accepts is free of remote spelling.

    The generated space mixes operands the safe-path policy already rejects with
    valid absolute ones, so the implication is exercised on both sides rather
    than being satisfied vacuously.
    """
    local = _LOCAL_OPERAND
    for role, arguments in (
        ("source", (candidate, local)),
        ("destination", (local, candidate)),
    ):
        try:
            argv = _rsync_argv(*arguments, RsyncOptions(allow_relative=True))
        except ValueError:
            continue
        emitted = _emitted_operand(argv, role)
        assert not _has_remote_spelling(emitted), (
            f"the builder emitted a remote {role}: {emitted!r}"
        )


@settings(max_examples=100)
@given(example=_GENERATED_REMOTE_OPERANDS)
def test_remote_strategy_examples_reach_the_sink_check(example: str) -> None:
    """The remote-operand strategy is non-vacuous.

    Each generated example must pass ``safe_path(allow_relative=True)`` and
    only then be rejected by the rsync-local check; otherwise the rejection
    property above could pass for the wrong reason.
    """
    assert safe_path(example, allow_relative=True), (
        "the strategy must generate operands safe_path accepts"
    )
    assert _has_remote_spelling(example), (
        "the generated operand must actually carry remote spelling"
    )
    with pytest.raises(ValueError, match="rsync source must be a local path"):
        _rsync_argv(example, _LOCAL_OPERAND, RsyncOptions(allow_relative=True))


# Option-shaped names in the forms the ticket names, with the ``./`` prefixes
# and redundant separators a caller might use. Stems carry the ``=`` form so
# ``./--rsh=x`` is reachable. Values avoid ":" and "..", so every generated
# name passes the generic policy and the remote predicate alike.
_GENERATED_OPTION_SHAPED = st.builds(
    lambda prefix, stem, value: f"{prefix}{stem}{value}",
    prefix=st.sampled_from(("", "./", ".//")),
    stem=st.sampled_from(("-e", "-esh", "--rsh", "-e=", "--rsh=")),
    value=st.text(alphabet="abcxyz09-_", min_size=0, max_size=6),
)


@settings(max_examples=200)
@given(operand=_GENERATED_OPTION_SHAPED, is_source=st.booleans())
def test_rsync_option_shaped_names_are_normalized_after_the_separator(
    operand: str,
    *,
    is_source: bool,
) -> None:
    """Generated option-shaped names stay operands, in normalized form.

    The non-vacuity check pins that the generic policy accepts every example
    and that none carries remote spelling. A rejection inside the builder would
    therefore be an over-broad policy rather than a defence, so the property
    asserts the documented acceptance as well as the separation.
    """
    normalized = str(safe_path(operand, allow_relative=True))
    assert not _has_remote_spelling(normalized), (
        f"the strategy must not generate remote spelling: {normalized!r}"
    )
    local = str(safe_path(_LOCAL_RELATIVE, allow_relative=True))
    expected = (normalized, local) if is_source else (local, normalized)
    role = "source" if is_source else "destination"
    argv = _rsync_argv(*expected, RsyncOptions(archive=True, allow_relative=True))

    assert argv == ("--archive", "--", *expected), (
        "exactly one separator must follow the flags and precede both operands"
    )
    emitted = _emitted_operand(argv, role)
    assert emitted == normalized, (
        f"the {role} must appear after the separator in normalized form"
    )
    assert not _has_remote_spelling(emitted), (
        f"the builder emitted a remote {role}: {emitted!r}"
    )
