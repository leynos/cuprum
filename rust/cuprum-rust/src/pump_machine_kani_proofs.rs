//! Bounded Kani proofs for the pure pump state machine.
//!
//! These proofs establish the invariants #84 calls for over unbounded byte
//! counts and arbitrary starting states: the running total is monotonic, the
//! writer latch is sticky, a closed writer drains without accruing bytes, and
//! the loop stops exactly on EOF.
//!
//! They drive [`advance`](super::advance) rather than the private `step`,
//! because `advance` is where the write precondition lives. Proving `step`
//! alone would establish nothing about a closed writer: the narrowed
//! `Transition` type makes an invalid combination unrepresentable, so the
//! guarantee is that `advance` never builds one — which is a property of
//! `advance`.

use std::convert::Infallible;

use super::{Flow, PumpState, WriteEvent, advance};

fn any_write() -> WriteEvent {
    let bytes: u64 = kani::any();
    if kani::any() {
        WriteEvent::Complete { bytes }
    } else {
        WriteEvent::Closed { bytes }
    }
}

/// Drive one iteration with a symbolic write, reporting whether it ran.
fn drive(state: &mut PumpState, read_len: usize, write: WriteEvent) -> (Flow, bool) {
    let mut invoked = false;
    let outcome = advance(state, read_len, || {
        invoked = true;
        Ok::<WriteEvent, Infallible>(write)
    });
    match outcome {
        Ok(flow) => (flow, invoked),
        Err(never) => match never {},
    }
}

#[kani::proof]
fn total_is_monotonic_across_a_step() {
    let mut state = PumpState::from_parts(kani::any(), kani::any());
    let before = state.total_written();
    drive(&mut state, kani::any(), any_write());
    kani::assert(
        state.total_written() >= before,
        "the running total never decreases",
    );
}

#[kani::proof]
fn loop_stops_exactly_on_eof() {
    let mut state = PumpState::from_parts(kani::any(), kani::any());
    let read_len: usize = kani::any();
    let (flow, _) = drive(&mut state, read_len, any_write());
    kani::assert(
        (flow == Flow::Stop) == (read_len == 0),
        "the loop stops iff the read returned zero bytes",
    );
}

#[kani::proof]
fn closed_writer_drains_without_writing() {
    // Start from an already-closed writer with an arbitrary running total.
    let mut state = PumpState::from_parts(kani::any(), false);
    let before = state.total_written();
    let (_, invoked) = drive(&mut state, kani::any(), any_write());
    kani::assert(!invoked, "a closed writer is never written to");
    kani::assert(!state.writer_open(), "a closed writer never reopens");
    kani::assert(
        state.total_written() == before,
        "a closed writer accrues no further bytes",
    );
}

#[kani::proof]
fn eof_never_writes() {
    // A zero-length read must stop without attempting a write, whatever the
    // writer's state.
    let mut state = PumpState::from_parts(kani::any(), kani::any());
    let before = state;
    let (flow, invoked) = drive(&mut state, 0, any_write());
    kani::assert(!invoked, "EOF must not attempt a write");
    kani::assert(flow == Flow::Stop, "EOF stops the loop");
    kani::assert(state == before, "EOF leaves the state unchanged");
}

#[kani::proof]
fn write_counts_bytes_and_latches_by_variant() {
    // Cover both write outcomes in one proof: either counts its accepted bytes,
    // and only a completed write leaves the writer open (a broken-pipe short
    // write latches it closed).
    let mut state = PumpState::from_parts(kani::any(), true);
    let before = state.total_written();
    let write = any_write();
    let bytes = match write {
        WriteEvent::Complete { bytes } | WriteEvent::Closed { bytes } => bytes,
    };
    let read_len: usize = kani::any();
    kani::assume(read_len != 0);
    let (_, invoked) = drive(&mut state, read_len, write);
    kani::assert(invoked, "an open writer and a chunk must perform the write");
    kani::assert(
        state.total_written() == before.saturating_add(bytes),
        "accepted bytes are counted for either write outcome",
    );
    kani::assert(
        state.writer_open() == matches!(write, WriteEvent::Complete { .. }),
        "only a completed write keeps the writer open",
    );
}

#[kani::proof]
#[kani::unwind(4)]
fn total_stays_monotonic_over_three_steps() {
    let mut state = PumpState::start();
    let mut before = state.total_written();
    for _ in 0..3 {
        drive(&mut state, kani::any(), any_write());
        kani::assert(
            state.total_written() >= before,
            "the running total never decreases across iterations",
        );
        before = state.total_written();
    }
}
