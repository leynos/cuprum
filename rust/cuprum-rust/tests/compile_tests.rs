//! Compile-time and UI tests for PyO3 patterns used by the maturin build.

#[test]
fn compile_time_ui() {
    let test_cases = trybuild::TestCases::new();
    test_cases.pass("tests/ui/pass/*.rs");
    test_cases.compile_fail("tests/ui/fail/*.rs");
}
