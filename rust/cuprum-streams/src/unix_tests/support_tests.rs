//! Rule cases for the isolation marker, driven through an injected lookup.

use super::ISOLATED_CHILD_ENV;
use crate::unix_tests::{EnvLookup, isolation_from};

/// A lookup that answers only for the isolation marker.
fn marker(value: Option<&'static str>) -> impl Fn(&str) -> Option<String> {
    move |name| {
        (name == ISOLATED_CHILD_ENV)
            .then(|| value.map(str::to_owned))
            .flatten()
    }
}

#[test]
fn an_absent_marker_does_not_isolate() {
    let lookup: EnvLookup<'_> = &marker(None);

    assert!(
        !isolation_from(lookup),
        "an absent marker must leave the raw descriptor probe off",
    );
}

#[test]
fn only_the_value_one_isolates() {
    let one: EnvLookup<'_> = &marker(Some("1"));
    let other: EnvLookup<'_> = &marker(Some("yes"));
    let empty: EnvLookup<'_> = &marker(Some(""));

    assert!(isolation_from(one), "`1` must enable isolation");
    assert!(
        !isolation_from(other),
        "any other value must leave the probe off",
    );
    assert!(!isolation_from(empty), "an empty value must not isolate");
}

#[test]
fn sink_expectation_needs_a_payload_for_the_payload_case() {
    let error = crate::unix_tests::support::sink_expectation("the payload", None)
        .expect_err("the payload case needs a recorded label");

    assert!(
        error.contains("no payload label was recorded"),
        "the failure must name the absent label, found {error}",
    );
}

#[test]
fn sink_expectation_refuses_an_unknown_word() {
    let error = crate::unix_tests::support::sink_expectation("something else", None)
        .expect_err("an unknown expectation must fail closed");

    assert!(
        error.contains("unknown sink expectation"),
        "the failure must name the word, found {error}",
    );
}

#[test]
fn observe_pump_keeps_a_semantic_failure_distinct() {
    let observed =
        crate::unix_tests::support::observe_pump(Err(crate::PumpError::BufferRangeExceeded));

    assert_eq!(
        observed,
        crate::unix_tests::support::ObservedPump::OtherFailure("BufferRangeExceeded".to_owned()),
        "a semantic failure must not be folded into the I/O variant",
    );
}
