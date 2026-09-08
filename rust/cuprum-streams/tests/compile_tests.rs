//! Compile contracts for safe stream policy.
#![forbid(unsafe_code)]
#[test]
fn transition_privacy() {
    trybuild::TestCases::new().compile_fail("tests/ui/fail/*.rs");
}
