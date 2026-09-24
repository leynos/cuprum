"""Contract tests for the benchmark ratchet's measurement protocol.

The ratchet's thresholds, its workload, its iteration count, and the rules that
decide when a main-branch sample is published are measurement protocol: they
decide what a recorded sample *means*. The ratchet compares only samples whose
profile metadata agrees, so a workflow that stated any of them differently from
the module that owns them would make every sample incomparable — silently,
since nothing about a mismatched sample looks wrong.

These declarations live in `.github/workflows/ci.yml`, which is why the tests
here read the workflow rather than the benchmark code. The declarations that
decide *whether* the ratchet runs at all are a separate contract, asserted in
`test_benchmark_gate_ci_contract.py`; how a main run publishes what it measured
is asserted from the other side, against the recorded artefacts, in
`test_benchmark_baseline_publication_contract.py`.
"""

from __future__ import annotations

import re

import pytest

from benchmarks.pipeline_throughput_scenarios import CI_RATCHET_WORKER_ITERATIONS
from benchmarks.ratchet_history import (
    DEFAULT_MAX_REGRESSION,
    DEFAULT_NOISE_SIGMAS,
    DEFAULT_WINDOW_SIZE,
)
from tests.helpers.workflow import BENCHMARK_JOB, Workflow, script_of, step_named
from tests.helpers.workflow_recipe import (
    flag_value,
    shell_function,
    shell_statements,
    top_level_operators,
)

#: The step that runs both the measurement and the ratchet comparison. Its two
#: shell functions are the measurement protocol, and the step that publishes a
#: main run's sample is conditioned on it.
THROUGHPUT_STEP = "Run throughput benchmarks and ratchet comparison"


#: The shell function in `THROUGHPUT_STEP` that runs the ratchet comparison.
RATCHET_FUNCTION = "run_ratchet"

#: The shell function in `THROUGHPUT_STEP` that measures, as distinct from the
#: one that judges. The measurement flags live here.
RATCHET_BENCHMARKS_FUNCTION = "run_ratchet_benchmarks"

#: Steps that publish what a `main` run measured: the sample the ratchet is
#: re-measured against, and the artefact pull-request runs compare with.
SAMPLE_STEP = "Record this run's benchmark sample"

BASELINE_UPLOAD_STEP = "Upload main benchmark baseline artifact"

#: The only step state the publication steps may read: whether this run produced
#: candidate artefacts at all. Reading anything else — the ratchet's outcome in
#: particular — is what would let a failing run withhold its own sample.
ARTEFACT_STEP = "candidate-artefacts"

ARTEFACT_AVAILABLE = f"steps.{ARTEFACT_STEP}.outputs.available == 'true'"

#: GitHub status functions that would make publication depend on the run's
#: outcome. `!cancelled()` is deliberately absent: it is the one status term
#: that runs whatever the verdict, and these steps require it.
STATUS_FUNCTIONS = ("success(", "failure(")


def _referenced_steps(condition: str) -> set[str]:
    """Return every step whose state a publication condition reads.

    GitHub expressions reach a step's state in two spellings — `steps.name.…`
    and `steps['name']…` — and the second is easy to overlook. A condition
    that withheld publication with `steps['ratchet'].outcome == 'success'`
    reads the ratchet's verdict while presenting nothing the dotted pattern
    can see, so both spellings are collected. The caller then requires the set
    to be exactly the steps publication is allowed to consult, which is a
    stronger statement than rejecting the reads someone thought to name.

    Parameters
    ----------
    condition : str
        A step's `if:` expression, with or without the `${{ }}` wrapper.

    Returns
    -------
    set[str]
        The identifiers the expression reads from `steps`, in either spelling.
    """
    dotted = set(re.findall(r"steps\.([A-Za-z0-9_-]+)", condition))
    bracketed = set(re.findall(r"steps\[\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\]", condition))
    # `outputs` and `result` follow the step identifier rather than root it, so
    # neither is captured above and neither is a step name.
    return dotted | bracketed


class TestTheRatchetPolicy:
    """The thresholds and counts the ratchet compares samples under."""

    def test_the_ratchet_policy_matches_the_module_defaults(
        self,
        workflow_data: Workflow,
    ) -> None:
        """Require the workflow's ratchet thresholds to be the module's own.

        `benchmarks/ratchet_history.py` owns the policy — the flat floor, the noise
        multiplier, and the window size — and the ratchet CLI defaults to those same
        values. The workflow restates all three anyway, so that the job reads as the
        policy it applies instead of inheriting whatever the module currently says.
        That restatement is only safe while something notices when the two drift:
        otherwise a change to the module leaves the job silently applying the old
        numbers, and the failure is expensive in both directions. A wider floor
        hides real regressions; a narrower one reinstates the false positives of
        issue #219.
        """
        script = script_of(step_named(workflow_data, BENCHMARK_JOB, THROUGHPUT_STEP))
        assert script is not None, f"the {THROUGHPUT_STEP!r} step must run a script"
        body = shell_function(script, RATCHET_FUNCTION, step=THROUGHPUT_STEP)

        max_regression = float(flag_value(body, "--max-regression"))
        assert max_regression == DEFAULT_MAX_REGRESSION, (
            f"--max-regression must be the {DEFAULT_MAX_REGRESSION!r} that "
            f"benchmarks/ratchet_history.py owns; found {max_regression!r}"
        )

        noise_sigmas = float(flag_value(body, "--noise-sigmas"))
        assert noise_sigmas == DEFAULT_NOISE_SIGMAS, (
            f"--noise-sigmas must be the {DEFAULT_NOISE_SIGMAS!r} that "
            f"benchmarks/ratchet_history.py owns; found {noise_sigmas!r}"
        )

        window_size = int(flag_value(body, "--history-window"))
        assert window_size == DEFAULT_WINDOW_SIZE, (
            f"--history-window must be the {DEFAULT_WINDOW_SIZE!r} that "
            f"benchmarks/ratchet_history.py owns; found {window_size!r}"
        )

    def test_the_ratchet_worker_iterations_match_the_scenario_default(
        self,
        workflow_data: Workflow,
    ) -> None:
        """Require the workflow's iteration count to be the scenario module's own.

        The count is measurement protocol, not a tuning dial: it is recorded in
        every sample and the ratchet only compares samples whose profile metadata
        agrees, so a workflow that measured at one count while the module's
        default named another would silently make every local reproduction
        incomparable rather than visibly wrong. `--worker-iterations` therefore
        appears in two places that must agree — the workflow's flag, and the
        default the CLI resolves for `--ci-ratchet` — and this asserts they do.
        """
        script = script_of(step_named(workflow_data, BENCHMARK_JOB, THROUGHPUT_STEP))
        assert script is not None, f"the {THROUGHPUT_STEP!r} step must run a script"
        body = shell_function(script, RATCHET_BENCHMARKS_FUNCTION, step=THROUGHPUT_STEP)

        iterations = int(flag_value(body, "--worker-iterations"))
        assert iterations == CI_RATCHET_WORKER_ITERATIONS, (
            f"--worker-iterations must be the {CI_RATCHET_WORKER_ITERATIONS!r} "
            f"that benchmarks/pipeline_throughput_scenarios.py owns; "
            f"found {iterations!r}"
        )


class TestTheRatchetMeasurement:
    """What the gate measures, and that a failed measurement aborts it."""

    def test_the_ratchet_benchmarks_measure_the_ci_ratchet_workload(
        self,
        workflow_data: Workflow,
    ) -> None:
        """Require the gate to measure the `--ci-ratchet` workload, not a smoke run.

        `run_ratchet_benchmarks` is the whole of what the gate measures, so the
        workload flag it passes is the gate's definition rather than a detail of
        it. `--smoke` is the tempting alternative — it exercises the same shape at
        smaller payloads and finishes sooner — but it measures a different payload
        than the one the recorded samples were taken at, and the ratchet only
        compares samples whose profile metadata agrees. Selecting the wrong one
        would leave every candidate incomparable with the window rather than
        visibly wrong.

        The whole-body check is what makes this more than a flag-presence test:
        `--ci-ratchet` and `--smoke` are mutually exclusive, so a body carrying
        both is not a valid invocation however the flags are spelled.
        """
        script = script_of(step_named(workflow_data, BENCHMARK_JOB, THROUGHPUT_STEP))
        assert script is not None, f"the {THROUGHPUT_STEP!r} step must run a script"
        body = shell_function(script, RATCHET_BENCHMARKS_FUNCTION, step=THROUGHPUT_STEP)

        assert "--ci-ratchet" in body, (
            f"{RATCHET_BENCHMARKS_FUNCTION!r} must measure the --ci-ratchet "
            f"workload; found: {body}"
        )
        assert "--smoke" not in body, (
            f"{RATCHET_BENCHMARKS_FUNCTION!r} must not pass --smoke, which selects "
            "a different payload than the recorded samples were measured at; "
            f"found: {body}"
        )

    def test_the_ratchet_benchmarks_invocation_propagates_failure(
        self,
        workflow_data: Workflow,
    ) -> None:
        """Require each measured command to abort the function when it fails.

        The confirmation path invokes this function as a condition operand, and
        Bash suspends `errexit` for a function body invoked that way. Without an
        explicit guard on each command, a failed `make develop` or a failed
        benchmark would fall through to the next command and the function would
        return the last command's status — so the confirmation branch would read a
        stale or missing plan as a measured result rather than as a failure. An
        explicit `set -e` inside the body does not restore aborting, which is why
        this pins the guard rather than trusting the shell option.
        """
        script = script_of(step_named(workflow_data, BENCHMARK_JOB, THROUGHPUT_STEP))
        assert script is not None, f"the {THROUGHPUT_STEP!r} step must run a script"
        body = shell_function(script, RATCHET_BENCHMARKS_FUNCTION, step=THROUGHPUT_STEP)

        statements = shell_statements(body)
        for marker in (
            "make develop MATURIN_DEVELOP_FLAGS",
            "benchmarks/pipeline_throughput.py",
            "benchmarks/ci_benchmark_ratchet_profile.py",
        ):
            matching = [stmt for stmt in statements if marker in stmt]
            assert matching, (
                f"{marker!r} must be invoked by {RATCHET_BENCHMARKS_FUNCTION!r}"
            )
            for statement in matching:
                assert statement.endswith("|| return $?"), (
                    f"the {marker!r} invocation in {RATCHET_BENCHMARKS_FUNCTION!r} "
                    "must guard itself with `|| return $?`, so its failure reaches "
                    "the caller instead of being masked by the next command: "
                    f"{statement}"
                )


class TestThePublicationCondition:
    """When a `main` run publishes its sample, and what it may read to decide."""

    @pytest.mark.parametrize(
        "step_name",
        [
            pytest.param(SAMPLE_STEP, id="record-sample"),
            pytest.param(BASELINE_UPLOAD_STEP, id="upload-baseline"),
        ],
    )
    def test_the_main_sample_is_published_whatever_the_ratchet_decides(
        self,
        workflow_data: Workflow,
        step_name: str,
    ) -> None:
        """Require publication to depend on the measurement, never on the verdict.

        Both steps publish what a `main` run measured. A window fed only by passing
        runs is a window of low-biased samples: a measurement faster than the bar is
        always accepted, while the slower measurements that would correct it are the
        ones a failing run would withhold. So the conditions must run on every
        completed run — `!cancelled()` rather than GitHub's implicit `success()` —
        and must read only whether candidate artefacts exist, never the ratchet's
        outcome.
        """
        condition = step_named(workflow_data, BENCHMARK_JOB, step_name).get("if")
        assert isinstance(condition, str), (
            f"the {step_name!r} step must declare an `if:` condition; "
            f"found {condition!r}"
        )

        assert "!cancelled()" in condition, (
            f"the {step_name!r} step must run after a failed ratchet as well as a "
            "passed one, so its condition needs `!cancelled()`; an interrupted run "
            f"that measured half a sample still publishes nothing. Found: {condition!r}"
        )
        for status_function in STATUS_FUNCTIONS:
            assert status_function not in condition, (
                f"the {step_name!r} step must not name {status_function!r}. "
                "Naming one is what makes publication depend on the run's outcome: "
                "`success()` withholds the sample of every run that measured a "
                "slowdown, which is the low-biased window of issue #219, and "
                "`failure()` publishes nothing at all. `!cancelled()` is the only "
                f"status term that runs whatever the verdict. Found: {condition!r}"
            )
        assert top_level_operators(condition, "||") == 0, (
            f"the {step_name!r} step must require all of its guards, not any of "
            "them: a top-level disjunction would let publication proceed with no "
            f"candidate artefacts. Found: {condition!r}"
        )
        assert top_level_operators(condition, "&&") >= 2, (
            f"the {step_name!r} step must combine its guards conjunctively, so "
            "that every one of them has to hold for the step to run. "
            f"Found: {condition!r}"
        )
        assert ARTEFACT_AVAILABLE in condition, (
            f"the {step_name!r} step must still require this run to have produced "
            f"candidate artefacts. Found: {condition!r}"
        )

        referenced = _referenced_steps(condition)
        assert referenced == {ARTEFACT_STEP}, (
            f"the {step_name!r} step may read only {ARTEFACT_STEP!r} step state; found "
            f"{sorted(referenced)}. Reading the ratchet's outcome here would withhold "
            "the sample of exactly the runs the window most needs, which is how the "
            f"baseline became low-biased (issue #219). Found: {condition!r}"
        )


class TestTheConditionReaders:
    """The readers the assertions above rest on, checked directly."""

    def test_the_guard_reader_splits_on_every_shell_separator(self) -> None:
        """A guard is attached to a command, not to the line carrying it.

        The test above only holds if the reader hands it commands. `make develop
        ...; true || return $?` is one physical line holding an unguarded command
        and a guarded one; a reader that split on newlines would report the whole
        line as a single guarded statement and pass. These are the shapes that
        distinguish the two, checked directly so the guard test's own reliability
        does not rest on the workflow happening to be laid out one command per
        line.
        """
        assert shell_statements("make develop X=x; true || return $?") == (
            "make develop X=x",
            "true || return $?",
        ), "an unquoted semicolon must end a statement"

        assert shell_statements("a || return $?; b || return $?") == (
            "a || return $?",
            "b || return $?",
        ), "each command on a shared line must be its own statement"

        assert shell_statements("sh -c 'echo a; echo b' || return $?") == (
            "sh -c 'echo a; echo b' || return $?",
        ), "a separator inside quotes is part of the command, not a split point"

        assert shell_statements("echo 'a # b' || return $?") == (
            "echo 'a # b' || return $?",
        ), "a hash inside quotes is part of the command, not a comment"

        assert shell_statements("cmd || return $?  # guarded deliberately") == (
            "cmd || return $?",
        ), "a trailing comment must be dropped without truncating the command"

        assert shell_statements("cmd a \\\n  || return $?") == (
            "cmd a || return $?",
        ), "a line continuation must join rather than split"

    def test_the_published_step_reader_sees_both_step_spellings(self) -> None:
        """A step may be read as `steps.name` or `steps['name']`, and both count.

        The publication test requires the steps its conditions read to be exactly
        `candidate-artefacts`. That requirement is only as strong as the reader
        behind it: a reader that matched the dotted spelling alone would pass a
        condition of the form `steps['ratchet'].outcome == 'success'`, which is a
        verdict-dependent condition wearing a spelling the reader cannot see. The
        two spellings are checked directly so the publication test's reliability
        does not rest on the workflow happening to use one of them.
        """
        assert _referenced_steps(
            "steps.candidate-artefacts.outputs.available == 'true'"
        ) == {"candidate-artefacts"}, "the dotted spelling must be read"

        assert _referenced_steps(
            "${{ !cancelled() && steps['ratchet'].outcome == 'success' }}"
        ) == {"ratchet"}, "the bracketed spelling must be read too"

        assert _referenced_steps(
            "${{ steps[\"ratchet\"].result == 'success' "
            "&& steps.candidate-artefacts.outputs.available == 'true' }}"
        ) == {"ratchet", "candidate-artefacts"}, (
            "double quotes must be recognized, and both spellings collected"
        )

        assert (
            _referenced_steps("${{ !cancelled() && github.ref == 'refs/heads/main' }}")
            == set()
        ), "a condition reading no step state must report none"
