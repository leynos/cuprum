//! Pure, `io::Error`-free model of the read/write pump loop.
//!
//! [`pump_stream_files_readwrite`](crate::pump_stream_files_readwrite) drives
//! real descriptor I/O, but its bug-prone logic is pure: how a read outcome and
//! a write outcome move the running byte total and the latched `writer_open`
//! flag. Extracting that decision here lets it be checked exhaustively with
//! proptest and Kani, free of descriptors and `io::Error`.
//!
//! Fatal read and write errors are handled by the caller — they short-circuit
//! the loop by propagating the real [`PumpError`](crate::errors::PumpError), so
//! they never reach this machine. It models only the non-fatal transitions:
//! reading a chunk, reaching EOF, a completed write, and the non-fatal
//! broken-pipe write that latches the writer closed.

/// What one loop iteration did, in a form that cannot express an impossible
/// combination.
///
/// The previous API took a read event and an `Option<WriteEvent>`
/// independently, so a caller could pass a write for an EOF read, or a write
/// performed after the writer had latched closed. Both were silently ignored,
/// which left the precondition living in a doc comment and in whatever each
/// caller happened to do. Here those states are not representable, and
/// [`advance`] is the only constructor.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Transition {
    /// A chunk was read while the writer was open, and this write resulted.
    Wrote(WriteEvent),
    /// A chunk was read after the writer latched closed, so the iteration only
    /// drains the upstream reader.
    Drained,
    /// The read returned zero bytes: end of input.
    Eof,
}

/// The non-fatal outcome of writing a chunk while the writer is open.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum WriteEvent {
    /// The whole chunk was accepted: `bytes` written and the writer stays open.
    Complete { bytes: u64 },
    /// A broken pipe or connection reset accepted `bytes` (possibly zero)
    /// before the writer latched closed.
    Closed { bytes: u64 },
}

/// The running state of the pump loop.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct PumpState {
    total_written: u64,
    writer_open: bool,
}

impl PumpState {
    /// The initial state: nothing written yet, writer open.
    pub(crate) const fn start() -> Self {
        Self {
            total_written: 0,
            writer_open: true,
        }
    }

    /// Build a state directly from its parts, for exhaustive verification from
    /// an arbitrary starting point.
    #[cfg(kani)]
    pub(crate) const fn from_parts(total_written: u64, writer_open: bool) -> Self {
        Self {
            total_written,
            writer_open,
        }
    }

    /// Bytes confirmed written downstream so far.
    pub(crate) const fn total_written(self) -> u64 {
        self.total_written
    }

    /// Whether the writer is still accepting data.
    pub(crate) const fn writer_open(self) -> bool {
        self.writer_open
    }
}

/// Whether the pump loop continues or stops after an iteration.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Flow {
    /// Keep reading.
    Continue,
    /// Stop: the read reached EOF.
    Stop,
}

/// Advance the machine by one loop iteration, writing only when permitted.
///
/// This is the pump's whole transition policy and the only production entry
/// point into the machine. It translates the raw read length — zero bytes is
/// end of input, anything else is a chunk — and runs `write_once` **only**
/// when a chunk was read while the writer is still open. On EOF, or once the
/// writer has latched closed, no write is attempted and the iteration merely
/// drains the reader.
///
/// Keeping the length translation and the write precondition together here
/// means the pump loop, the property tests, and the bounded proofs exercise
/// one definition of them rather than three copies.
///
/// A closed writer never reopens and never accrues more bytes.
///
/// # Errors
///
/// Propagates whatever `write_once` returns, so a fatal I/O failure aborts the
/// iteration before the state is advanced.
///
/// # Examples
///
/// A completed write accrues its bytes and keeps pumping:
///
/// ```rust,ignore
/// let mut state = PumpState::start();
/// let flow = advance(&mut state, 5, || {
///     Ok::<_, Infallible>(WriteEvent::Complete { bytes: 5 })
/// })?;
/// assert_eq!(flow, Flow::Continue);
/// assert_eq!(state.total_written(), 5);
/// assert!(state.writer_open());
/// ```
///
/// A broken pipe latches the writer closed, after which chunk reads drain the
/// reader without writing again, and a zero-length read stops the loop:
///
/// ```rust,ignore
/// advance(&mut state, 4, || Ok::<_, Infallible>(WriteEvent::Closed { bytes: 2 }))?;
/// assert!(!state.writer_open());
///
/// // The writer is closed, so this closure is never called.
/// advance(&mut state, 4, || unreachable!("a closed writer is never written to"))?;
/// assert_eq!(state.total_written(), 2, "a drained chunk adds nothing");
///
/// assert_eq!(advance(&mut state, 0, || unreachable!())?, Flow::Stop);
/// ```
pub(crate) fn advance<E>(
    state: &mut PumpState,
    read_len: usize,
    write_once: impl FnOnce() -> Result<WriteEvent, E>,
) -> Result<Flow, E> {
    let transition = if read_len == 0 {
        Transition::Eof
    } else if state.writer_open() {
        Transition::Wrote(write_once()?)
    } else {
        Transition::Drained
    };
    Ok(step(state, transition))
}

/// Apply one transition to the state and report whether the loop continues.
///
/// There is deliberately no `writer_open` check here: [`Transition::Wrote`]
/// can only exist for a chunk read while the writer was open, because
/// [`advance`] is its only constructor. The precondition is enforced once,
/// where the decision is made, rather than re-checked defensively at the point
/// of use — a second check would silently mask a caller that got it wrong.
fn step(state: &mut PumpState, transition: Transition) -> Flow {
    match transition {
        Transition::Eof => Flow::Stop,
        Transition::Drained => Flow::Continue,
        Transition::Wrote(event) => {
            apply_write(state, event);
            Flow::Continue
        }
    }
}
/// Apply a write outcome to the running total and the `writer_open` latch.
fn apply_write(state: &mut PumpState, write: WriteEvent) {
    match write {
        WriteEvent::Complete { bytes } => {
            state.total_written = state.total_written.saturating_add(bytes);
        }
        WriteEvent::Closed { bytes } => {
            state.total_written = state.total_written.saturating_add(bytes);
            state.writer_open = false;
        }
    }
}

#[cfg(test)]
#[path = "pump_machine_tests.rs"]
mod tests;

#[cfg(kani)]
#[path = "pump_machine_kani_proofs.rs"]
mod kani_proofs;
