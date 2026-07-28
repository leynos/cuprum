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
fn broken_pipe_latches_closed_and_counts_accepted_bytes() {
    let mut state = PumpState::from_parts(kani::any(), true);
    let before = state.total_written();
    let bytes: u64 = kani::any();
    step(
        &mut state,
        ReadEvent::Chunk,
        Some(WriteEvent::Closed { bytes }),
    );
    kani::assert(
        !state.writer_open(),
        "a non-fatal short write latches the writer closed",
    );
    kani::assert(
        state.total_written() == before.saturating_add(bytes),
        "bytes accepted before the pipe broke are counted",
    );
}

#[kani::proof]
fn completed_write_keeps_open_and_counts_bytes() {
    let mut state = PumpState::from_parts(kani::any(), true);
    let before = state.total_written();
    let bytes: u64 = kani::any();
    step(
        &mut state,
        ReadEvent::Chunk,
        Some(WriteEvent::Complete { bytes }),
    );
    kani::assert(
        state.writer_open(),
        "a completed write keeps the writer open",
    );
    kani::assert(
        state.total_written() == before.saturating_add(bytes),
        "written bytes are counted",
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
