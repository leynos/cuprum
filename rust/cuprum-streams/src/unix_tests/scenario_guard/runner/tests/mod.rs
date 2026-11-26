//! Classification and resolution cases, including every rejection shape.

use std::{
    collections::BTreeMap,
    path::Path,
    process::Command,
    time::{Duration, Instant},
};

use super::{ChildReport, classify, resolve};
use crate::unix_tests::ScenarioExpectation;

const NAME: &str = "unix_tests::pump_transfers_a_preloaded_payload_to_the_sink";

fn expectation(title: &'static str, binding: &'static str) -> ScenarioExpectation {
    ScenarioExpectation {
        title,
        binding,
        requires_writer_probe: false,
    }
}

/// Build a report from a run whose scenario succeeded.
fn report<'a>(stdout: &'a str, stderr: &'a str) -> ChildReport<'a> {
    ChildReport {
        test_name: NAME,
        succeeded: true,
        stdout,
        stderr,
        requires_witness: false,
    }
}

fn passing_output() -> String {
    format!(
        "running 1 test\ntest {NAME} ... ok\n\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 \
         measured; 0 filtered out; finished in 0.00s\n",
    )
}

#[test]
fn accepts_a_single_passing_run() {
    assert_eq!(
        classify(&report(&passing_output(), "")),
        Ok(()),
        "a run reporting one passing test is acceptable evidence",
    );
}

#[test]
fn rejects_a_run_that_selected_nothing() {
    let output = "running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; \
                  1 filtered out; finished in 0.00s\n";

    let error =
        classify(&report(output, "")).expect_err("a run that selected nothing must not pass");
    assert!(
        error.contains("exactly one running test"),
        "the failure must name the empty run, found {error}",
    );
}

#[test]
fn rejects_an_ignored_result() {
    let output = format!(
        "running 1 test\ntest {NAME} ... ignored\n\ntest result: ok. 0 passed; 0 failed; 1 \
         ignored; 0 measured; 0 filtered out\n",
    );

    let error =
        classify(&report(&output, "")).expect_err("an ignored scenario must not count as executed");
    assert!(
        error.contains("did not report"),
        "the failure must name the missing result line, found {error}",
    );
}

#[test]
fn rejects_a_failed_result() {
    let output = format!(
        "running 1 test\ntest {NAME} ... FAILED\n\ntest result: FAILED. 0 passed; 1 failed\n",
    );

    let failed = ChildReport {
        succeeded: false,
        ..report(&output, "")
    };
    let error = classify(&failed).expect_err("a failing scenario must not count as executed");
    assert!(
        error.contains("exited unsuccessfully"),
        "the failure must report the exit status, found {error}",
    );
}

#[test]
fn rejects_a_missing_witness() {
    let output = passing_output();
    let watched = ChildReport {
        requires_witness: true,
        ..report(&output, "")
    };
    let error = classify(&watched).expect_err("a writer-probe scenario must print its witness");
    assert!(
        error.contains("writer-probe witness"),
        "the failure must name the missing witness, found {error}",
    );
}

#[test]
fn accepts_a_present_witness() {
    let witness = crate::unix_tests::support::WRITER_PROBE_WITNESS;

    assert_eq!(
        classify(&ChildReport {
            requires_witness: true,
            ..report(&passing_output(), witness)
        }),
        Ok(()),
        "the witness must satisfy the writer-probe requirement",
    );
}

#[test]
fn resolves_one_generated_test_per_binding() {
    let listed = vec![
        "unix_tests::first_case".to_owned(),
        "unix_tests::second_case".to_owned(),
        "unix_tests::scenario_guard::tests::unrelated".to_owned(),
    ];
    let expected = [
        expectation("First case", "first_case"),
        expectation("Second case", "second_case"),
    ];

    let resolved = resolve(&listed, &expected).expect("both bindings resolve");

    assert_eq!(
        resolved.get("first_case").map(String::as_str),
        Some("unix_tests::first_case"),
        "the first binding must resolve to its generated name",
    );
    assert_eq!(
        resolved.get("second_case").map(String::as_str),
        Some("unix_tests::second_case"),
        "the second binding must resolve to its generated name",
    );
}

#[test]
fn reports_a_missing_generated_test() {
    let error = resolve(&[], &[expectation("First case", "first_case")])
        .expect_err("an unregistered binding must be reported");
    assert!(
        error.contains("no generated test ends with"),
        "the failure must name the missing test, found {error}",
    );
}

#[test]
fn reports_an_ambiguous_generated_test() {
    let listed = ["a::first_case".to_owned(), "b::first_case".to_owned()];

    let error = resolve(&listed, &[expectation("First case", "first_case")])
        .expect_err("an ambiguous binding must be reported");
    assert!(
        error.contains("one binding must resolve to one test"),
        "the failure must explain the ambiguity, found {error}",
    );
}
#[test]
fn render_status_names_an_exit_code() {
    let status = Command::new("/bin/sh")
        .args(["-c", "exit 7"])
        .status()
        .expect("the probe command must run");
    assert_eq!(
        super::render_status(status),
        "exit code 7",
        "a normal exit must render its code",
    );
}

#[test]
fn render_status_falls_back_to_the_debug_form_without_a_code() {
    // A signal-terminated child has no exit code, so the fallback branch is
    // reachable only through a real signal.
    let mut child = Command::new("/bin/sh")
        .args(["-c", "kill -TERM $$"])
        .spawn()
        .expect("the probe command must start");
    let status = child.wait().expect("the probe command must be reaped");
    assert!(
        !super::render_status(status).contains("exit code"),
        "a signalled child has no exit code to render",
    );
}

#[test]
fn list_tests_reports_a_failing_binary() {
    let error = super::list_tests(Path::new("/bin/sh"))
        .expect_err("a shell is not a test binary and exits non-zero");

    assert!(
        error.contains("listing the test names failed"),
        "the failure must name the listing step, found {error}",
    );
}

#[test]
fn run_isolated_reports_a_failing_binary() {
    let error = super::run_isolated(Path::new("/bin/sh"), "some::test", false)
        .expect_err("a shell is not a test binary and exits non-zero");

    assert!(
        error.contains("a shell is not a test binary") || error.contains("status="),
        "the failure must carry the child's diagnostics, found {error}",
    );
}

#[test]
fn run_bounded_reports_a_missing_program() {
    let error = super::run_bounded(
        Command::new("/nonexistent/probe"),
        Duration::from_millis(50),
    )
    .expect_err("a missing program must fail to start");

    assert!(
        error.contains("failed to start"),
        "the failure must name the spawn, found {error}",
    );
}

#[test]
fn run_bounded_kills_a_child_that_outlives_its_deadline() {
    let mut command = Command::new("/bin/sh");
    command.args(["-c", "sleep 30"]);
    let started = Instant::now();

    let captured = super::run_bounded(command, Duration::from_millis(100))
        .expect("a killed child is still captured");

    assert!(
        captured.timed_out,
        "the child must be reported as timed out"
    );
    assert!(
        started.elapsed() < Duration::from_secs(10),
        "the deadline must bound the wait, not the child",
    );
}

#[test]
fn reject_unclaimed_reports_a_name_no_scenario_claims() {
    let listed = [
        "unix_tests::pump_case".to_owned(),
        "unix_tests::stray".to_owned(),
    ];
    let resolved = BTreeMap::from([("pump_case", "unix_tests::pump_case".to_owned())]);

    let error = super::reject_unclaimed(&listed, &resolved)
        .expect_err("an unclaimed direct child must be reported");

    assert!(
        error.contains("stray"),
        "the failure must name the unclaimed test, found {error}",
    );
}

#[test]
fn reject_unclaimed_ignores_nested_unit_test_modules() {
    let listed = [
        "unix_tests::pump_case".to_owned(),
        "unix_tests::support::tests::a_case".to_owned(),
        "unix_tests::scenario_guard::a_case".to_owned(),
    ];
    let resolved = BTreeMap::from([("pump_case", "unix_tests::pump_case".to_owned())]);

    assert_eq!(
        super::reject_unclaimed(&listed, &resolved),
        Ok(()),
        "nested unit tests and the guard's own tests are not scenarios",
    );
}

#[test]
fn record_outcome_counts_a_pass_and_files_a_failure() {
    let scenario = ScenarioExpectation {
        title: "Probe scenario",
        binding: "probe_binding",
        requires_writer_probe: false,
    };
    let mut tally = super::Tally::default();

    super::record_outcome(
        Path::new("/bin/sh"),
        &super::Attempt {
            scenario: &scenario,
            test_name: "unix_tests::probe_binding",
        },
        &mut tally,
    );

    assert_eq!(
        tally.executed, 0,
        "a failing child must not count as executed"
    );
    assert_eq!(
        tally.failures.len(),
        1,
        "a failing child must file exactly one failure",
    );
    let reason = tally.failures.first().map_or("", String::as_str);
    assert!(
        reason.contains("Probe scenario"),
        "the failure must name the scenario, found {reason}",
    );
}
