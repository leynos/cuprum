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
