"""The unsafe-bearing crate allowlist must reject unreviewed workspace growth."""

from pathlib import Path

import pytest

from scripts.check_boundary_contract import check_members, check_safe_policy


@pytest.mark.parametrize("extra", [", 'new-boundary'", ", 'cuprum-native-io'", ", '*'"])
def test_unreviewed_members_are_rejected(extra: str) -> None:
    """Extra, repeated, and wildcard members all require an explicit audit."""
    manifest = (
        "[workspace]\nmembers = ['cuprum-rust', 'cuprum-native-io', 'cuprum-streams'"
        + extra
        + "]"
    )
    with pytest.raises(ValueError, match="audited boundary inventory"):
        check_members(manifest)


def test_audited_members_are_accepted() -> None:
    """The documented three-crate dependency split is accepted."""
    check_members(
        "[workspace]\nmembers = ['cuprum-rust', 'cuprum-native-io', 'cuprum-streams']"
    )


@pytest.mark.parametrize("replacement", ["deny", "allow"])
def test_safe_targets_cannot_weaken_unsafe_policy(replacement: str) -> None:
    """A target-wide deny can be locally relaxed, so only forbid is accepted."""
    root = Path(__file__).resolve().parents[2] / "rust"
    workspace = (root / "Cargo.toml").read_text(encoding="utf-8")
    safe = (root / "cuprum-streams/Cargo.toml").read_text(encoding="utf-8")
    check_safe_policy(workspace, safe)
    weakened = safe.replace('unsafe_code = "forbid"', f'unsafe_code = "{replacement}"')
    with pytest.raises(ValueError, match="forbid unsafe in all targets"):
        check_safe_policy(workspace, weakened)
