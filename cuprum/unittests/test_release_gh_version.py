"""The GitHub CLI version gate that guards release provenance verification.

``release.yml`` pipes ``gh --version`` into ``scripts/release_gh_version.py``
before verifying any attestation. An older CLI cannot be trusted to enforce the
signer-workflow and source-ref policy, so the gate must fail closed on an old
release and on output it cannot parse.
"""

from __future__ import annotations

import pytest

from scripts.release_gh_version import MINIMUM_VERSION, main, parse_version

_MINIMUM = ".".join(str(part) for part in MINIMUM_VERSION)


@pytest.mark.parametrize(
    ("version", "is_accepted"),
    [
        ("2.101.9", False),
        ("2.102.0", True),
        ("2.102.1", True),
        ("3.0.0", True),
    ],
    ids=["below", "at-the-minimum", "above", "next-major"],
)
def test_only_a_recent_enough_cli_passes(
    version: str, *, is_accepted: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """The minimum release and later pass silently; earlier ones fail."""
    status = main([f"gh version {version} (2026-01-15)", "https://example.test"])

    captured = capsys.readouterr()
    assert status == (0 if is_accepted else 1)
    if is_accepted:
        assert not captured.out, "success must print nothing"
        assert not captured.err, "success must warn about nothing"
        return
    assert f"GitHub CLI {_MINIMUM} or newer is required; found {version}." in (
        captured.err
    ), "the gate must name both the minimum and the found release"


@pytest.mark.parametrize(
    "lines",
    [[], [""], ["gh version development build"], ["command not found: gh"]],
    ids=["no-output", "blank-line", "no-release-number", "foreign-error"],
)
def test_unparsable_output_fails_closed(
    lines: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Output that names no release cannot authorize verification."""
    assert main(lines) == 1, "unparsable output must fail the step"
    assert f"GitHub CLI {_MINIMUM} or newer is required" in capsys.readouterr().err


def test_only_the_first_line_is_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``gh --version`` prints a release link second, which is not the answer."""
    status = main(["gh version 2.97.0 (2026-07-31)", "gh version 9.9.9"])

    assert status == 1, "a later line must not rescue an old first line"
    assert "found 2.97.0" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("gh version 2.102.0 (2026-01-15)", (2, 102, 0)),
        ("gh version 10.20.30-beta1 (2027-01-01)", (10, 20, 30)),
        ("gh version 2.102 (2026-01-15)", None),
        ("", None),
    ],
    ids=["release", "suffix", "incomplete", "empty"],
)
def test_parse_version_reads_only_a_complete_release(
    line: str, expected: tuple[int, int, int] | None
) -> None:
    """A complete numeric release is parsed; anything else reads as unknown."""
    assert parse_version(line) == expected
