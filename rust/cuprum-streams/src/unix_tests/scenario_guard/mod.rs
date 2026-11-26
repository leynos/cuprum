//! Guards that keep the Unix stream scenarios honest.
//!
//! Two failure modes previously let a behavioural suite look complete while
//! proving nothing. A binding that omits `name` silently runs the *first*
//! declaration, so a feature file can declare five scenarios and execute one;
//! and a scenario whose steps never run still reports a passing test. This
//! module therefore checks two separate things:
//!
//! - **Parity.** The feature declarations, the named `#[scenario]` bindings, and the expectation
//!   table must describe the same scenarios. Nothing may be declared-but-unbound or
//!   bound-but-undeclared.
//! - **Execution.** Each expected scenario is re-run alone in a child process with an isolation
//!   marker, and the child's own output must report exactly one passing test. Child isolation is
//!   what makes the raw descriptor liveness probe meaningful: in this shared binary another thread
//!   can allocate a just-freed descriptor number before it is observed.

mod parsing;
mod runner;

use parsing::{ParityTable, bound_scenarios, check_parity, declared_scenarios};

use super::{FEATURE_PATH, FEATURE_TEXT, SCENARIOS};

/// The module source, included so the binding parser reads this test's code.
const MODULE_SOURCE: &str = include_str!("../mod.rs");

/// Every declared scenario must be bound by name and listed in the table.
#[test]
fn every_scenario_is_declared_bound_and_expected() {
    let declared = expect_ok(
        declared_scenarios(FEATURE_TEXT),
        "the feature file must parse",
    );
    let bound = expect_ok(
        bound_scenarios(MODULE_SOURCE, FEATURE_PATH),
        "the scenario bindings must parse",
    );
    let rows: Vec<(&str, &str)> = SCENARIOS
        .iter()
        .map(|entry| (entry.title, entry.binding))
        .collect();
    let table = ParityTable::from_rows(&rows);

    expect_ok(
        check_parity(&declared, &bound, &table),
        "the feature, the bindings, and the expectation table must agree",
    );
}

/// Re-run every expected scenario alone and prove each one executed.
#[test]
fn every_scenario_executes_in_isolation() {
    let executed = runner::execute_every_scenario(SCENARIOS);

    expect_ok(executed, "every scenario must run and pass in isolation");
}

/// Extract a guard result, failing the test with the guard's own diagnosis.
fn expect_ok<T>(result: Result<T, String>, context: &str) -> T {
    match result {
        Ok(value) => value,
        Err(reason) => panic!("{context}: {reason}"),
    }
}
