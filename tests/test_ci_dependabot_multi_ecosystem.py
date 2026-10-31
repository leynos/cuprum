"""Contracts for the multi-ecosystem Dependabot group that pairs maturin and PyO3.

maturin (uv) and PyO3 (Cargo) are versioned independently yet build one
extension, so an update to either alone cannot be built against the other. A
multi-ecosystem group puts the available updates for the two into one pull
request, so when both have one a single CI run builds both. It does not hold
back a bump when only one of the two has an update, and a group cannot choose
compatible versions: it coordinates the update without proving it. A mistake here is silent, because Dependabot
reports nothing for a stanza that names a group the file never defines, so the
checked-in configuration is read back and held to the arrangement.
"""

from __future__ import annotations

import typing as typ

import pytest
import yaml

from tests.test_ci_dependabot_config import CONFIG_PATH, INTERVAL, dependabot_stanzas

#: The group the two stanzas join.
MATURIN_PYO3_GROUP = "maturin-pyo3"

#: The dependencies each ecosystem contributes to that group.
MATURIN_PYO3_MEMBERS = {"uv": ["maturin"], "cargo": ["pyo3"]}


def _groups() -> dict[str, typ.Any]:
    """Return the top-level ``multi-ecosystem-groups`` mapping, or fail."""
    document = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    groups = document.get("multi-ecosystem-groups")
    assert isinstance(groups, dict), (
        f"{CONFIG_PATH} must define `multi-ecosystem-groups`; got {groups!r}"
    )
    return typ.cast("dict[str, typ.Any]", groups)


def test_the_maturin_pyo3_group_is_defined_with_a_daily_schedule() -> None:
    """Define the group the two stanzas join, on the estate's daily cadence."""
    groups = _groups()
    assert MATURIN_PYO3_GROUP in groups, (
        f"`multi-ecosystem-groups` must define {MATURIN_PYO3_GROUP!r}; "
        f"got {sorted(groups)}"
    )
    schedule = groups[MATURIN_PYO3_GROUP].get("schedule", {})
    assert schedule.get("interval") == INTERVAL, (
        f"the {MATURIN_PYO3_GROUP} group must run {INTERVAL!r}; "
        f"got {schedule.get('interval')!r}"
    )


@pytest.mark.parametrize(
    ("ecosystem", "members"),
    [pytest.param(*item, id=item[0]) for item in MATURIN_PYO3_MEMBERS.items()],
)
def test_each_half_of_the_extension_build_joins_the_group(
    ecosystem: str, members: list[str]
) -> None:
    """Name the group and the member in the stanza, or the pair arrives split."""
    stanza = dependabot_stanzas()[ecosystem]
    assert stanza.get("multi-ecosystem-group") == MATURIN_PYO3_GROUP, (
        f"the {ecosystem} stanza must join {MATURIN_PYO3_GROUP!r}; "
        f"got {stanza.get('multi-ecosystem-group')!r}"
    )
    assert stanza.get("patterns") == members, (
        f"the {ecosystem} stanza must offer exactly {members} to the group; "
        f"got {stanza.get('patterns')!r}"
    )


def test_no_other_stanza_joins_a_multi_ecosystem_group() -> None:
    """Keep the group to the two halves of the extension build."""
    joined = {
        ecosystem
        for ecosystem, stanza in dependabot_stanzas().items()
        if "multi-ecosystem-group" in stanza
    }
    assert joined == set(MATURIN_PYO3_MEMBERS), (
        f"only {sorted(MATURIN_PYO3_MEMBERS)} may join {MATURIN_PYO3_GROUP!r}; "
        f"got {sorted(joined)}"
    )
