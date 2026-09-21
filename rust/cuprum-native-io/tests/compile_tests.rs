//! Compile-time contracts for Windows synchronous-handle capabilities.
//!
//! Constructing a genuine overlapped handle is cumbersome and unstable in CI,
//! so this test pins the safer, durable guard: generic borrowed handles cannot
//! reach the synchronous native adapter through its safe API.

#[cfg(windows)]
#[test]
fn generic_borrowed_handles_are_rejected() {
    let cases = trybuild::TestCases::new();
    cases.compile_fail("tests/ui/fail/*.rs");
}
