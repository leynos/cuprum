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

/// The non-fatal outcome of a read from the upstream descriptor.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ReadEvent {
    /// A non-empty chunk was read.
    Chunk,
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

/// Advance the pump state by one loop iteration.
///
/// `write` carries the write outcome iff the caller performed a write this
/// iteration — that is, iff `read` is a [`ReadEvent::Chunk`] and the writer is
/// open. On EOF, or when a chunk is read while the writer has already latched
/// closed (the reader drain), `write` is `None`.
///
/// A closed writer never reopens and never accrues more bytes: once the writer
/// latches closed, further chunk reads only drain the upstream reader.
///
/// # Examples
///
/// A completed write accrues its bytes and keeps pumping:
///
/// ```rust,ignore
/// let mut state = PumpState::start();
/// let flow = step(
///     &mut state,
///     ReadEvent::Chunk,
///     Some(WriteEvent::Complete { bytes: 5 }),
/// );
/// assert_eq!(flow, Flow::Continue);
/// assert_eq!(state.total_written(), 5);
/// assert!(state.writer_open());
/// ```
///
/// A broken pipe latches the writer closed, after which chunk reads drain the
/// reader without accruing bytes, and EOF stops the loop:
///
/// ```rust,ignore
/// let mut state = PumpState::start();
/// step(&mut state, ReadEvent::Chunk, Some(WriteEvent::Closed { bytes: 2 }));
/// assert!(!state.writer_open());
///
/// // The writer is closed, so the caller performs no write: `write` is `None`.
/// step(&mut state, ReadEvent::Chunk, None);
/// assert_eq!(state.total_written(), 2, "a drained chunk adds nothing");
///
/// assert_eq!(step(&mut state, ReadEvent::Eof, None), Flow::Stop);
/// ```
pub(crate) fn step(state: &mut PumpState, read: ReadEvent, write: Option<WriteEvent>) -> Flow {
    match read {
        ReadEvent::Eof => Flow::Stop,
        ReadEvent::Chunk => {
            if state.writer_open {
                if let Some(event) = write {
                    apply_write(state, event);
                }
            }
            // A closed writer drains the reader: the state is left unchanged.
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
mod tests {
    //! Property tests for the pure pump state machine.

    use proptest::prelude::*;

    use super::{Flow, PumpState, ReadEvent, WriteEvent, step};

    /// Drive one iteration exactly as `pump_stream_files_readwrite` does: a
    /// write is applied only when a chunk is read while the writer is open.
    fn drive(state: &mut PumpState, read: ReadEvent, write: WriteEvent) -> Flow {
        let performs_write = matches!(read, ReadEvent::Chunk) && state.writer_open();
        step(state, read, performs_write.then_some(write))
    }

    fn read_event() -> impl Strategy<Value = ReadEvent> {
        prop_oneof![Just(ReadEvent::Chunk), Just(ReadEvent::Eof)]
    }

    fn write_event() -> impl Strategy<Value = WriteEvent> {
        prop_oneof![
            (0_u64..=1 << 20).prop_map(|bytes| WriteEvent::Complete { bytes }),
            (0_u64..=1 << 20).prop_map(|bytes| WriteEvent::Closed { bytes }),
        ]
    }

    proptest! {
        /// Across any script of iterations the running total never decreases,
        /// the writer never reopens once closed, a closed writer accrues no
        /// further bytes, and the loop stops exactly on EOF.
        #[test]
        fn invariants_hold_across_iterations(
            script in prop::collection::vec((read_event(), write_event()), 0..64),
        ) {
            let mut state = PumpState::start();
            for (read, write) in script {
                let before = state;
                let flow = drive(&mut state, read, write);

                prop_assert!(
                    state.total_written() >= before.total_written(),
                    "total bytes must be monotonic",
                );
                prop_assert!(
                    before.writer_open() || !state.writer_open(),
                    "a closed writer must never reopen",
                );
                if !before.writer_open() {
                    prop_assert_eq!(
                        state.total_written(),
                        before.total_written(),
                        "a closed writer must accrue no further bytes",
                    );
                }
                prop_assert_eq!(
                    flow == Flow::Stop,
                    read == ReadEvent::Eof,
                    "the loop stops exactly on EOF",
                );
                // EOF halts the loop before any further state change.
                if read == ReadEvent::Eof {
                    prop_assert_eq!(state, before, "EOF leaves the state unchanged");
                }
            }
        }

        /// Once a broken-pipe write latches the writer closed, no later write
        /// outcome — however large — can increase the total again.
        #[test]
        fn writes_stop_after_broken_pipe(
            accepted in 0_u64..=1 << 20,
            tail in prop::collection::vec(write_event(), 0..32),
        ) {
            let mut state = PumpState::start();
            // First chunk latches the writer closed via a non-fatal short write.
            drive(&mut state, ReadEvent::Chunk, WriteEvent::Closed { bytes: accepted });
            prop_assert!(!state.writer_open());
            let latched_total = state.total_written();

            for write in tail {
                drive(&mut state, ReadEvent::Chunk, write);
                prop_assert!(!state.writer_open(), "writer stays closed");
                prop_assert_eq!(
                    state.total_written(),
                    latched_total,
                    "no bytes are written after the pipe breaks",
                );
            }
        }
    }
}

#[cfg(kani)]
#[path = "pump_machine_kani_proofs.rs"]
mod kani_proofs;
