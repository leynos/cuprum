//! Compile-fail UI test: an invalid pump transition cannot be fabricated from
//! outside `pump_machine`.
//!
//! `step` deliberately omits a `writer_open` check, and the Kani proofs drive
//! `advance` rather than `step`, because both rest on the same claim: a
//! `Transition::Wrote` can only exist for a chunk read while the writer was
//! open, since `advance` is its only constructor. That claim holds only while
//! `Transition` and `step` stay private to their module, so this case pins it.
//!
//! The real module source is included here so the probe below sits in the
//! module's parent — exactly the relationship `lib.rs` has with
//! `pump_machine`, and exactly where a future caller would sit. Widening
//! `Transition` or `step` even to `pub(crate)` changes what the compiler
//! reports here, so this case fails.
//!
//! `WriteEvent` and `PumpState` are `pub(crate)`, so they resolve; only the
//! transition type and its applicator do not. That is the boundary under test.

// The workspace declares `cfg(kani)` via `[workspace.lints.rust]`, but the
// throwaway crate trybuild builds around this file inherits no lint table, so
// the module's `#[cfg(kani)]` gates would otherwise fill the expected stderr
// with noise unrelated to the boundary under test.
#![allow(unexpected_cfgs, reason = "trybuild's crate inherits no check-cfg")]

#[path = "../../../src/pump_machine.rs"]
mod pump_machine;

fn main() {
    let mut state = pump_machine::PumpState::start();
    // The combination the machine forbids: a write recorded for an iteration
    // whose writer had already latched closed.
    let write = pump_machine::WriteEvent::Complete { bytes: 1 };
    let transition = pump_machine::Transition::Wrote(write);
    let _ = pump_machine::step(&mut state, transition);
}
