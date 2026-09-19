"""Contracts for the Dependabot update configuration.

``github-actions-lint`` reads ``.github/workflows`` only, so nothing in the
gate set reads ``.github/dependabot.yml``. A mistake in it is silent rather
than loud: Dependabot reports nothing at all for a ``directory`` that holds no
manifest of the declared ecosystem, so a stanza pointed at the wrong path is
indistinguishable from one that is merely quiet, and a dropped label surfaces
much later as an unlabelled pull request rather than as a failure.

These tests read the checked-in configuration back and check each stanza
against the manifests the repository actually has on disk.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest
import yaml

from tests.helpers.ci_workflows import ROOT

if typ.TYPE_CHECKING:
    from pathlib import Path

CONFIG_PATH = ROOT / ".github" / "dependabot.yml"

#: The schema version Dependabot requires before it reads ``updates``.
REQUIRED_VERSION = 2

#: The label every stanza carries so dependency pull requests stay filterable
#: as a single class, whatever ecosystem raised them.
BASELINE_LABEL = "dependencies"

#: The wildcard that makes a group match every dependency in its ecosystem.
EVERYTHING = "*"


@dc.dataclass(frozen=True, slots=True)
class ExpectedStanza:
    """What the estate baseline requires of one ecosystem's stanza."""

    ecosystem: str
    directory: str
    #: A path, relative to the stanza's ``directory``, that the repository
    #: keeps on disk. Without it the stanza updates nothing and says nothing.
    manifest: str
    #: The channel labels the stanza adds on top of the baseline label.
    channel_labels: tuple[str, ...]
    #: Whether the stanza batches its updates into one pull request.
    grouped: bool = False


#: One stanza per package ecosystem the repository uses. Listing them here
#: means a new ecosystem must be added deliberately, and an existing one
#: cannot be dropped without a failure naming it.
EXPECTED_STANZAS = (
    ExpectedStanza(
        ecosystem="github-actions",
        directory="/",
        manifest=".github/workflows",
        channel_labels=("github-actions",),
        grouped=True,
    ),
    ExpectedStanza(
        ecosystem="uv",
        directory="/",
        manifest="pyproject.toml",
        channel_labels=("python", "uv"),
    ),
    ExpectedStanza(
        ecosystem="cargo",
        directory="/rust",
        manifest="Cargo.toml",
        channel_labels=("cargo",),
    ),
)

#: Parametrized so a failure names the ecosystem it concerns.
STANZA_CASES = tuple(
    pytest.param(stanza, id=stanza.ecosystem) for stanza in EXPECTED_STANZAS
)


def _require(*, condition: bool, message: str) -> None:
    """Raise a contract failure when ``condition`` does not hold."""
    if not condition:
        raise AssertionError(message)


def _mapping(value: object, message: str) -> dict[str, object]:
    """Narrow a parsed YAML value to a string-keyed mapping."""
    _require(
        condition=isinstance(value, dict)
        and all(isinstance(key, str) for key in value),
        message=message,
    )
    return typ.cast("dict[str, object]", value)


def _sequence(value: object, message: str) -> list[object]:
    """Narrow a parsed YAML value to a list."""
    _require(condition=isinstance(value, list), message=message)
    return typ.cast("list[object]", value)


def _labels(stanza: dict[str, object], ecosystem: str) -> tuple[str, ...]:
    """Return a stanza's labels as text, failing on a non-string entry."""
    raw = _sequence(
        stanza.get("labels"),
        f"the {ecosystem} stanza must declare `labels` as a list",
    )
    labels = tuple(
        _text(
            entry,
            f"the {ecosystem} stanza has a non-string label: {entry!r}",
        )
        for entry in raw
    )
    _require(
        condition=len(labels) > 0,
        message=f"the {ecosystem} stanza must declare at least one label",
    )
    return labels


def _text(value: object, message: str) -> str:
    """Narrow a parsed YAML scalar to text."""
    _require(condition=isinstance(value, str), message=message)
    return typ.cast("str", value)


def dependabot_stanzas() -> dict[str, dict[str, object]]:
    """Return each update stanza keyed by its package ecosystem.

    Returns
    -------
    dict[str, dict[str, object]]
        One entry per ``updates`` list item.

    Raises
    ------
    AssertionError
        If the file does not parse as the Dependabot v2 schema, or if two
        stanzas claim the same ecosystem.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from _require()
    document = _mapping(
        yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")),
        f"{CONFIG_PATH} must parse to a mapping",
    )
    version = document.get("version")
    _require(
        condition=version == REQUIRED_VERSION,
        message=(
            f"{CONFIG_PATH} must declare `version: {REQUIRED_VERSION}`; got {version!r}"
        ),
    )
    updates = _sequence(
        document.get("updates"),
        f"{CONFIG_PATH} must declare an `updates` list",
    )
    stanzas: dict[str, dict[str, object]] = {}
    for index, entry in enumerate(updates):
        stanza = _mapping(entry, f"updates[{index}] must be a mapping")
        ecosystem = _text(
            stanza.get("package-ecosystem"),
            f"updates[{index}] must name a `package-ecosystem`",
        )
        _require(
            condition=ecosystem not in stanzas,
            message=(
                f"`{ecosystem}` is declared by more than one stanza; "
                "Dependabot would open duplicate pull requests"
            ),
        )
        stanzas[ecosystem] = stanza
    return stanzas


def test_every_ecosystem_in_use_has_exactly_one_stanza() -> None:
    """Declare one stanza per ecosystem, and no others."""
    declared = set(dependabot_stanzas())
    expected = {stanza.ecosystem for stanza in EXPECTED_STANZAS}
    assert declared == expected, (
        "each package ecosystem the repository uses needs exactly one stanza; "
        f"missing: {sorted(expected - declared)}; "
        f"unexpected: {sorted(declared - expected)}"
    )


@pytest.mark.parametrize("stanza", STANZA_CASES)
def test_each_stanza_watches_a_directory_holding_its_manifest(
    stanza: ExpectedStanza,
) -> None:
    """Point each stanza where its manifest really is."""
    declared = dependabot_stanzas()[stanza.ecosystem]
    directory = _text(
        declared.get("directory"),
        f"the {stanza.ecosystem} stanza must declare a `directory`",
    )
    assert directory == stanza.directory, (
        f"the {stanza.ecosystem} stanza must target {stanza.directory!r}; "
        f"got {directory!r}"
    )
    watched = _watched_path(stanza)
    assert watched.exists(), (
        f"the {stanza.ecosystem} stanza watches {directory!r}, which holds no "
        f"{stanza.manifest!r}; Dependabot would find nothing and report nothing"
    )


@pytest.mark.parametrize("stanza", STANZA_CASES)
def test_each_stanza_carries_the_baseline_and_channel_labels(
    stanza: ExpectedStanza,
) -> None:
    """Label every update so it is filterable by class and by ecosystem."""
    declared = dependabot_stanzas()[stanza.ecosystem]
    labels = _labels(declared, stanza.ecosystem)
    required = (BASELINE_LABEL, *stanza.channel_labels)
    missing = [label for label in required if label not in labels]
    assert not missing, (
        f"the {stanza.ecosystem} stanza must carry {list(required)}; "
        f"missing {missing}; got {list(labels)}"
    )


def test_the_uv_stanza_targets_the_workspace_root() -> None:
    """Keep the uv stanza on the directory that holds ``pyproject.toml``."""
    stanza = dependabot_stanzas()["uv"]
    assert stanza.get("directory") == "/", (
        f"the uv stanza must target the workspace root; got {stanza.get('directory')!r}"
    )
    assert _labels(stanza, "uv") == (
        BASELINE_LABEL,
        "python",
        "uv",
    ), "the uv stanza must carry the dependencies, python, and uv labels"


def test_github_actions_updates_are_batched_into_one_pull_request() -> None:
    """Collapse every action bump into a single pull request."""
    stanza = dependabot_stanzas()["github-actions"]
    groups = _mapping(
        stanza.get("groups"),
        "the github-actions stanza must declare `groups` to batch updates",
    )
    assert len(groups) == 1, (
        "one group keeps every action bump in a single pull request; "
        f"got {sorted(groups)}"
    )
    for name, group in groups.items():
        patterns = _sequence(
            _mapping(group, f"group {name!r} must be a mapping").get("patterns"),
            f"group {name!r} must declare `patterns`",
        )
        assert EVERYTHING in patterns, (
            f"group {name!r} must match every dependency with {EVERYTHING!r}; "
            f"got {patterns}"
        )


@pytest.mark.parametrize("stanza", STANZA_CASES)
def test_each_stanza_bounds_its_pull_request_count(stanza: ExpectedStanza) -> None:
    """Cap concurrent pull requests so updates cannot crowd out review."""
    declared = dependabot_stanzas()[stanza.ecosystem]
    limit = declared.get("open-pull-requests-limit")
    _require(
        condition=isinstance(limit, int) and limit > 0,
        message=(
            f"the {stanza.ecosystem} stanza needs a positive "
            f"`open-pull-requests-limit`; got {limit!r}"
        ),
    )


def _watched_path(stanza: ExpectedStanza) -> Path:
    """Return the on-disk path a stanza's ``directory`` resolves to.

    Returns
    -------
    Path
        The absolute path Dependabot would search for the manifest.
    """
    # `Path` treats a leading slash as absolute, so the Dependabot-relative
    # form has to be stripped before it can be joined under the repository.
    return ROOT / stanza.directory.strip("/") / stanza.manifest
