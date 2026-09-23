"""Contract tests for the actionlint runner-label registry.

actionlint knows GitHub's own labels natively but not a self-hosted provider's,
so an unregistered label is not a lint error: the job simply queues for a
runner that never arrives. The registry is therefore asserted as an equality in
both directions against the labels the workflows actually resolve to, because a
subset assertion misses a stale registration and a vendor-prefix derivation
would exempt a second paid provider's labels from the question entirely.
"""

from __future__ import annotations

import re

import yaml

from tests.helpers.ci_placement import FROZEN_HOSTED_LABELS, declares_steps, placement
from tests.helpers.ci_runners import ROOT, all_jobs, workflow_sources

ACTIONLINT_CONFIG = ROOT / ".github" / "actionlint.yaml"
#: A `vars.*` reference in a workflow. actionlint checks such a reference
#: against `config-variables`, so the declared list and the references read
#: back from the workflows have to agree in both directions: an undeclared
#: name is a lint error, and a declared name nothing reads is a stale
#: allowance that would let a deleted variable's typo pass.
CONFIGURATION_VARIABLE = re.compile(r"vars\.([A-Za-z_][A-Za-z0-9_]*)")
ALL_CASES = all_jobs()
#: The GitHub-hosted labels this repository actually runs on, maintained
#: independently of `FROZEN_HOSTED_LABELS`. The frozen set is an allowance
#: list, so a misspelling added to both it and a matrix leg would satisfy every
#: assertion that reads one against the other; this second list has to be
#: edited too, and it is an equality rather than a membership test.
HOSTED_LABELS_IN_USE = frozenset({
    "macos-15-intel",
    "macos-latest",
    "ubuntu-latest",
    "windows-2022",
})


def _labels_in_use() -> frozenset[str]:
    """Return every runner label the estate's jobs can resolve to.

    Derived from every job, both arms of a conditional and every matrix runner
    entry, because a registry assertion is only as complete as its derivation.
    Netsuke's contract searched the concatenated workflow text for each
    registered label, so a label named only in a comment explaining why a lane
    no longer used it stayed registered for ever.

    Returns
    -------
    frozenset[str]
        Every label any step-declaring job in the estate can resolve to.
    """
    labels: set[str] = set()
    for workflow_name, job_name in ALL_CASES:
        if not declares_steps(workflow_name, job_name):
            # A reusable-workflow caller declares no runner. Exempt by `uses`,
            # not by a missing `runs-on`: a job with neither is still refused
            # by the reader (whitaker #438).
            continue
        labels |= placement(workflow_name, job_name).labels
    return frozenset(labels)


def test_actionlint_registers_exactly_the_self_hosted_labels_in_use() -> None:
    """Register intentional labels so a typo fails lint instead of queueing.

    Equality in both directions. A subset assertion misses a stale
    registration, and deriving "in use" by a vendor prefix would silently
    exempt a second paid provider's labels from the registry question
    altogether, which is the substantive defect; the named frozen set and the
    prefix filter agree over today's workflows and differ on exactly that case.
    actionlint knows GitHub's own labels natively, so the frozen set is
    subtracted rather than registered (whitaker #438).
    """
    config = yaml.safe_load(ACTIONLINT_CONFIG.read_text(encoding="utf-8"))
    declared = config["self-hosted-runner"]["labels"]
    used = _labels_in_use()
    unknown = used - frozenset(FROZEN_HOSTED_LABELS)
    assert sorted(declared) == sorted(unknown), (
        "actionlint must register every label in use that is neither a "
        f"GitHub-hosted label nor already registered; declared {sorted(declared)}, "
        f"derived {sorted(unknown)}"
    )
    # Carried forward from main, which replaced a hardcoded
    # `["CODESCENE_CLI_SHA256"]` with a derivation after that variable's last
    # reader was deleted. Keeping the old literal through this rebase would
    # have silently reverted that fix and re-asserted a variable nothing reads.
    #
    # Compared as sorted lists: the config registers names for lint to
    # resolve, and actionlint does not care what order they appear in. Sorting
    # still fails on a duplicate, so the comparison loses nothing.
    read = {
        match.group(1)
        for _, source in workflow_sources()
        for match in CONFIGURATION_VARIABLE.finditer(source)
    }
    assert sorted(config["config-variables"]) == sorted(read), (
        "list only the configuration variables the workflows read, so a typo "
        f"in a vars.* reference fails lint; declared "
        f"{config['config-variables']}, read {sorted(read)}"
    )


def test_every_hosted_label_in_use_is_named_in_the_frozen_set() -> None:
    """Keep the frozen set a closed list rather than an implicit tolerance.

    Without this the registry equality above could be satisfied by widening
    `FROZEN_HOSTED_LABELS` to swallow a paid provider's label, which is the
    failure the named set exists to prevent.
    """
    hosted = _labels_in_use() & frozenset(FROZEN_HOSTED_LABELS)
    assert hosted == HOSTED_LABELS_IN_USE, (
        "the GitHub-hosted labels in use must match the independently "
        f"maintained list; in use {sorted(hosted)}, expected "
        f"{sorted(HOSTED_LABELS_IN_USE)}. A misspelt label added to both a "
        "matrix leg and the frozen set fails here, where it would otherwise "
        "leave a job queued for a runner that does not exist"
    )
    config = yaml.safe_load(ACTIONLINT_CONFIG.read_text(encoding="utf-8"))
    registered = frozenset(config["self-hosted-runner"]["labels"])
    assert not (hosted & registered), (
        "a GitHub-hosted label must not also be registered as self-hosted; "
        f"both claim {sorted(hosted & registered)}"
    )
    assert hosted == _labels_in_use() - registered, (
        "every label in use must be either registered or named in the frozen "
        f"hosted set; unaccounted: {sorted(_labels_in_use() - registered - hosted)}"
    )


def test_no_retired_runner_labels_remain() -> None:
    """Leave no Namespace label or cache action behind after the migration."""
    for workflow_name, source in workflow_sources():
        assert "namespace-profile" not in source, (
            f"{workflow_name} still references a Namespace runner profile"
        )
        assert "nscloud" not in source, (
            f"{workflow_name} still references the Namespace cache action"
        )
