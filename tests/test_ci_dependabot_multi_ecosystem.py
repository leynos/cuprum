"""Contracts for the multi-ecosystem Dependabot group that pairs maturin and PyO3.

maturin (uv) and PyO3 (Cargo) are versioned independently yet build one
extension, so an update to either alone cannot be built against the other. A
multi-ecosystem group puts the available updates for the two into one pull
request, so when both have one a single CI run builds both. It does not hold
back a bump when only one of the two has an update, and a group cannot choose
compatible versions: it coordinates the update without proving it.

Dependabot builds one job per entry, and an entry that joins a group runs only
that group, over the dependencies its ``patterns`` take. Cuprum's first run of
the arrangement showed it: the entries' own groups, ignore rules and every
other dependency were not processed. So each of the two ecosystems has two
entries, the group entry and a plain entry that ignores the pair so that it is
handled once. A mistake here is silent, because Dependabot reports nothing for
a stanza that names a group the file never defines, so the checked-in
configuration is read back and held to the arrangement. Dependabot also rejects
``open-pull-requests-limit`` on an entry that joins a group, so the bound sits
on the group.
"""

from __future__ import annotations

import typing as typ

import pytest
import yaml

from tests.test_ci_dependabot_config import (
    CONFIG_PATH,
    INTERVAL,
    STANZA_CASES,
    ExpectedStanza,
    dependabot_stanzas,
)

#: The group the two stanzas join.
MATURIN_PYO3_GROUP = "maturin-pyo3"

#: The dependencies each ecosystem contributes to that group.
MATURIN_PYO3_MEMBERS = {"uv": ["maturin"], "cargo": ["pyo3"]}


def _document() -> dict[str, typ.Any]:
    """Return the parsed Dependabot configuration."""
    document = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{CONFIG_PATH} must parse to a mapping"
    return typ.cast("dict[str, typ.Any]", document)


def _groups() -> dict[str, typ.Any]:
    """Return the top-level ``multi-ecosystem-groups`` mapping, or fail."""
    groups = _document().get("multi-ecosystem-groups")
    assert isinstance(groups, dict), (
        f"{CONFIG_PATH} must define `multi-ecosystem-groups`; got {groups!r}"
    )
    return typ.cast("dict[str, typ.Any]", groups)


def _group_entries() -> list[dict[str, typ.Any]]:
    """Return every ``updates`` entry that joins a multi-ecosystem group."""
    return [
        entry for entry in _document()["updates"] if "multi-ecosystem-group" in entry
    ]


def test_the_maturin_pyo3_group_is_defined_with_a_daily_schedule() -> None:
    """Define the group the two entries join, on the estate's daily cadence."""
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
def test_each_half_of_the_extension_build_has_one_group_entry(
    ecosystem: str, members: list[str]
) -> None:
    """Name the group and the member in one entry per ecosystem, or the pair splits."""
    entries = [
        entry for entry in _group_entries() if entry["package-ecosystem"] == ecosystem
    ]
    assert len(entries) == 1, (
        f"the {ecosystem} ecosystem needs exactly one entry joining "
        f"{MATURIN_PYO3_GROUP!r}; got {len(entries)}"
    )
    (entry,) = entries
    assert entry["multi-ecosystem-group"] == MATURIN_PYO3_GROUP, (
        f"the {ecosystem} group entry must join {MATURIN_PYO3_GROUP!r}; "
        f"got {entry['multi-ecosystem-group']!r}"
    )
    assert entry.get("patterns") == members, (
        f"the {ecosystem} group entry must offer exactly {members} to the group; "
        f"got {entry.get('patterns')!r}"
    )
    assert "groups" not in entry, (
        f"the {ecosystem} group entry runs only the group, so it carries no `groups`"
    )


@pytest.mark.parametrize(
    ("ecosystem", "members"),
    [pytest.param(*item, id=item[0]) for item in MATURIN_PYO3_MEMBERS.items()],
)
def test_the_plain_entry_ignores_the_pair_the_group_entry_handles(
    ecosystem: str, members: list[str]
) -> None:
    """Keep the pair out of the plain entry, which handles everything else."""
    rules = typ.cast(
        "list[dict[str, typ.Any]]", dependabot_stanzas()[ecosystem].get("ignore", [])
    )
    ignored = [rule.get("dependency-name") for rule in rules]
    assert ignored == members, (
        f"the plain {ecosystem} entry must ignore exactly {members}, which the "
        f"group entry handles; got {ignored}"
    )


def test_only_the_extension_build_ecosystems_join_a_group() -> None:
    """Keep the group to the two halves of the extension build."""
    joined = {entry["package-ecosystem"] for entry in _group_entries()}
    assert joined == set(MATURIN_PYO3_MEMBERS), (
        f"only {sorted(MATURIN_PYO3_MEMBERS)} may join {MATURIN_PYO3_GROUP!r}; "
        f"got {sorted(joined)}"
    )


@pytest.mark.parametrize("stanza", STANZA_CASES)
def test_each_plain_stanza_bounds_its_pull_request_count(
    stanza: ExpectedStanza,
) -> None:
    """Cap concurrent pull requests so updates cannot crowd out review."""
    limit = dependabot_stanzas()[stanza.ecosystem].get("open-pull-requests-limit")
    assert isinstance(limit, int), (
        f"the {stanza.ecosystem} stanza needs an integer "
        f"`open-pull-requests-limit`; got {limit!r}"
    )
    assert limit > 0, (
        f"the {stanza.ecosystem} stanza needs a positive "
        f"`open-pull-requests-limit`; got {limit!r}"
    )


def test_the_group_bounds_its_pull_request_count_and_its_entries_do_not() -> None:
    """Dependabot rejects the bound on a group entry, so the group carries it."""
    limit = _groups()[MATURIN_PYO3_GROUP].get("open-pull-requests-limit")
    assert isinstance(limit, int), (
        f"the {MATURIN_PYO3_GROUP} group needs an integer "
        f"`open-pull-requests-limit`; got {limit!r}"
    )
    assert limit > 0, (
        f"the {MATURIN_PYO3_GROUP} group needs a positive "
        f"`open-pull-requests-limit`; got {limit!r}"
    )
    carrying = [
        entry["package-ecosystem"]
        for entry in _group_entries()
        if "open-pull-requests-limit" in entry
    ]
    assert not carrying, (
        f"Dependabot rejects the bound on {carrying}; set it on the group"
    )
