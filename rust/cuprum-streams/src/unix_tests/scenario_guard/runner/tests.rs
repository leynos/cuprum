//! Classification and resolution cases, including every rejection shape.

//! Classification and resolution cases, including every rejection shape.

use super::{classify, resolve};
use crate::unix_tests::ScenarioExpectation;

const NAME: &str = "unix_tests::pump_transfers_a_preloaded_payload_to_the_sink";

fn expectation(title: &'static str, binding: &'static str) -> ScenarioExpectation {
    ScenarioExpectation {
        title,
        binding,
        requires_writer_probe: false,
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
        classify(NAME, true, &passing_output(), "", false),
        Ok(()),
        "a run reporting one passing test is acceptable evidence",
    );
}

#[test]
fn rejects_a_run_that_selected_nothing() {
    let output = "running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; \
                  1 filtered out; finished in 0.00s\n";

    let error = classify(NAME, true, output, "", false)
        .expect_err("a run that selected nothing must not pass");
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

    let error = classify(NAME, true, &output, "", false)
        .expect_err("an ignored scenario must not count as executed");
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

    let error = classify(NAME, false, &output, "", false)
        .expect_err("a failing scenario must not count as executed");
    assert!(
        error.contains("exited unsuccessfully"),
        "the failure must report the exit status, found {error}",
    );
}

#[test]
fn rejects_a_missing_witness() {
    let error = classify(NAME, true, &passing_output(), "", true)
        .expect_err("a writer-probe scenario must print its witness");
    assert!(
        error.contains("writer-probe witness"),
        "the failure must name the missing witness, found {error}",
    );
}

#[test]
fn accepts_a_present_witness() {
    let witness = crate::unix_tests::support::WRITER_PROBE_WITNESS;

    assert_eq!(
        classify(NAME, true, &passing_output(), witness, true),
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
    let listed = vec!["a::first_case".to_owned(), "b::first_case".to_owned()];

    let error = resolve(&listed, &[expectation("First case", "first_case")])
        .expect_err("an ambiguous binding must be reported");
    assert!(
        error.contains("one binding must resolve to one test"),
        "the failure must explain the ambiguity, found {error}",
    );
}
