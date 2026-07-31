//! Bounded Kani proofs for the FD-borrow ownership contract.
//!
//! The invariant under proof, over every modelled exit path:
//!
//! - a reader passed through `with_borrowed_reader` is **never** closed by
//!   Rust — it stays caller-owned on normal completion and on unwind;
//! - the `pump_stream` writer is **consumed** and closes exactly once on
//!   every exit path, which is what signals EOF downstream.
//!
//! Scope. These are bounded-model proofs over the ownership *model* in
//! [`super::fd_ownership_model`], not over real descriptors: Kani does not
//! interpret I/O, so no real `close(2)` effect is exercised here. Actual
//! descriptor behaviour stays covered by the real-FD regression tests in
//! `lib_tests.rs`. Tracking issue: <https://github.com/leynos/cuprum/issues/89>.
//!
//! Bounds. The exit space is finite and fully enumerated (two modes, both
//! reached — see the `kani::cover!` statements). Only
//! [`repeated_borrows_never_close_the_reader`] loops, and its iteration count
//! is bounded explicitly.

use super::fd_ownership_model::{
    CloseLog, ExitMode, ModelFd, model_consume_stream, model_pump_stream,
    model_with_borrowed_reader,
};

/// Nondeterministically choose an exit mode, covering both alternatives.
fn any_exit_mode() -> ExitMode {
    if kani::any::<bool>() {
        ExitMode::Normal
    } else {
        ExitMode::Unwind
    }
}

/// `pump_stream`: the reader survives every exit path and the writer closes
/// on every exit path.
///
/// Loop-free: the only nondeterminism is the two-valued exit mode.
#[kani::proof]
fn pump_borrows_reader_and_consumes_writer() {
    let reader_log = CloseLog::new();
    let writer_log = CloseLog::new();
    let exit = any_exit_mode();

    // Prove both boundary cases are reachable, so neither assertion below is
    // vacuously satisfied by an unreachable branch.
    kani::cover!(exit == ExitMode::Normal, "reaches normal completion");
    kani::cover!(exit == ExitMode::Unwind, "reaches the modelled unwind exit");

    let outcome = model_pump_stream(ModelFd::new(&reader_log), ModelFd::new(&writer_log), exit);

    kani::assert(
        reader_log.closes() == 0,
        "a borrowed reader FD is never closed by Rust, on any exit path",
    );
    kani::assert(
        writer_log.closes() == 1,
        "the pump writer is consumed and closes exactly once, signalling EOF",
    );
    kani::assert(
        outcome.is_err() == (exit == ExitMode::Unwind),
        "the modelled unwind exit propagates out of the helper",
    );
}

/// `consume_stream`: its only descriptor is a borrowed reader, which survives
/// every exit path.
///
/// Loop-free: the only nondeterminism is the two-valued exit mode.
#[kani::proof]
fn consume_borrows_its_only_reader() {
    let reader_log = CloseLog::new();
    let exit = any_exit_mode();

    kani::cover!(exit == ExitMode::Normal, "reaches normal completion");
    kani::cover!(exit == ExitMode::Unwind, "reaches the modelled unwind exit");

    let outcome = model_consume_stream(ModelFd::new(&reader_log), exit);

    kani::assert(
        reader_log.closes() == 0,
        "a borrowed reader FD is never closed by Rust, on any exit path",
    );
    kani::assert(
        outcome.is_err() == (exit == ExitMode::Unwind),
        "the modelled unwind exit propagates out of the helper",
    );
}

/// Borrowing the same descriptor repeatedly never accumulates a close, so no
/// sequence of borrows can produce the double close the contract rules out.
///
/// Bound: at most three borrows, each with an independently chosen exit mode;
/// `unwind(4)` is one greater than the maximum iteration count.
#[kani::proof]
#[kani::unwind(4)]
fn repeated_borrows_never_close_the_reader() {
    const MAX_BORROWS: usize = 3;

    let reader_log = CloseLog::new();
    let borrows: usize = kani::any();
    kani::assume(borrows <= MAX_BORROWS);

    kani::cover!(borrows == 0, "reaches the zero-borrow boundary");
    kani::cover!(
        borrows == MAX_BORROWS,
        "reaches the maximum-borrow boundary"
    );

    for _ in 0..borrows {
        let exit = any_exit_mode();
        let outcome =
            model_with_borrowed_reader(ModelFd::new(&reader_log), |_reader| exit.outcome());
        kani::assert(
            outcome.is_err() == (exit == ExitMode::Unwind),
            "each borrow reports its own exit mode",
        );
    }

    kani::assert(
        reader_log.closes() == 0,
        "repeated borrows of one reader FD never close it",
    );
}

/// Non-vacuity witness: an owning handle that nothing suppresses *does*
/// record a close, so the zero-close assertions above are meaningful rather
/// than artefacts of a model that never closes anything.
#[kani::proof]
fn an_unsuppressed_owning_handle_closes() {
    let log = CloseLog::new();
    drop(ModelFd::new(&log));
    kani::assert(
        log.closes() == 1,
        "dropping an owning handle records exactly one close",
    );
}
