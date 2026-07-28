//! Bounded Kani proofs for the pure pump state machine.
//!
//! These proofs establish the invariants #84 calls for over unbounded byte
//! counts and arbitrary starting states: the running total is monotonic, the
//! writer latch is sticky, a closed writer drains without accruing bytes, and
//! the loop stops exactly on EOF.

use super::{Flow, PumpState, ReadEvent, WriteEvent, step};

fn any_read() -> ReadEvent {
    if kani::any() {
        ReadEvent::Chunk
    } else {
        ReadEvent::Eof
    }
}

fn any_write() -> WriteEvent {
    let bytes: u64 = kani::any();
    if kani::any() {
        WriteEvent::Complete { bytes }
    } else {
        WriteEvent::Closed { bytes }
    }
}

fn any_opt_write() -> Option<WriteEvent> {
    if kani::any() { Some(any_write()) } else { None }
}

#[kani::proof]
fn total_is_monotonic_across_a_step() {
    let mut state = PumpState::from_parts(kani::any(), kani::any());
    let before = state.total_written();
    step(&mut state, any_read(), any_opt_write());
    kani::assert(
        state.total_written() >= before,
        "the running total never decreases",
    );
}

#[kani::proof]
fn loop_stops_exactly_on_eof() {
    let mut state = PumpState::from_parts(kani::any(), kani::any());
    let read = any_read();
    let flow = step(&mut state, read, any_opt_write());
    kani::assert(
        (flow == Flow::Stop) == (read == ReadEvent::Eof),
        "the loop stops iff the read reached EOF",
    );
}

#[kani::proof]
fn closed_writer_drains_without_writing() {
    // Start from an already-closed writer with an arbitrary running total.
    let mut state = PumpState::from_parts(kani::any(), false);
    let before = state.total_written();
    step(&mut state, any_read(), any_opt_write());
    kani::assert(!state.writer_open(), "a closed writer never reopens");
    kani::assert(
        state.total_written() == before,
        "a closed writer accrues no further bytes",
    );
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
    step(&mut state, ReadEvent::Chunk, Some(write));
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
        step(&mut state, any_read(), any_opt_write());
        kani::assert(
            state.total_written() >= before,
            "the running total never decreases across iterations",
        );
        before = state.total_written();
    }
}
