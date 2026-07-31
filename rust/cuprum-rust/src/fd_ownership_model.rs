//! Pure ownership model for the borrowed-reader / consumed-writer contract.
//!
//! Kani cannot model real operating-system descriptor effects: closing an FD
//! is I/O, which the bounded model checker does not interpret. This module
//! therefore replaces the descriptor with [`ModelFd`], whose `Drop` records a
//! close in a [`CloseLog`] instead of issuing one. Everything else — the
//! [`ManuallyDrop`] wrapper, the closure call, the early-exit edge — is real
//! Rust, so Rust's own drop elaboration decides the outcome rather than any
//! hand-written accounting.
//!
//! # Modelling the unwind edge
//!
//! The hazard `with_borrowed_reader` exists to prevent only appears when a
//! panic unwinds *through the helper's own frame*: the superseded
//! `mem::forget`-after-the-call pattern was skipped on that path, so the
//! caller-owned reader was dropped and closed. Kani builds with panics as
//! aborts, so a literal `panic!` cannot be used to reach that path.
//!
//! [`ModelUnwind`] and the `?` operator stand in for it. Both a `?` early
//! return and a real unwind leave the frame *without executing the statements
//! that follow the operation*, and both run drop glue for every live local.
//! That correspondence is what gives the proofs their teeth: a `mem::forget`
//! placed after the operation is skipped by `?` exactly as it is skipped by an
//! unwind, so reintroducing the old pattern makes the proofs fail.

use core::cell::Cell;
use core::mem::ManuallyDrop;

/// Records how many times the modelled descriptor was closed.
///
/// A real close is an unmodellable side effect, so the model counts them.
#[derive(Debug, Default)]
pub(crate) struct CloseLog {
    closes: Cell<u32>,
}

impl CloseLog {
    /// Create a log with no recorded closes.
    pub(crate) const fn new() -> Self {
        Self {
            closes: Cell::new(0),
        }
    }

    /// Number of closes recorded so far.
    pub(crate) fn closes(&self) -> u32 {
        self.closes.get()
    }

    fn record_close(&self) {
        self.closes.set(self.closes.get().saturating_add(1));
    }
}

/// A modelled *owning* descriptor handle.
///
/// Dropping it records a close, mirroring `OwnedFd` on Unix and `File` on
/// Windows. Suppressing that drop is precisely what the borrow helper must
/// guarantee for a caller-owned reader.
#[derive(Debug)]
pub(crate) struct ModelFd<'log> {
    log: &'log CloseLog,
}

impl<'log> ModelFd<'log> {
    /// Reconstruct an owning handle that reports closes to `log`.
    pub(crate) const fn new(log: &'log CloseLog) -> Self {
        Self { log }
    }
}

impl Drop for ModelFd<'_> {
    fn drop(&mut self) {
        self.log.record_close();
    }
}

/// Stands in for a panic-unwind leaving the frame early.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct ModelUnwind;

/// How a modelled operation leaves its scope.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ExitMode {
    /// The operation ran to completion and returned normally.
    Normal,
    /// The operation aborted, standing in for a panic-unwind through the
    /// helper's frame.
    Unwind,
}

impl ExitMode {
    /// Outcome an operation returns under this exit mode.
    pub(crate) const fn outcome(self) -> Result<(), ModelUnwind> {
        match self {
            Self::Normal => Ok(()),
            Self::Unwind => Err(ModelUnwind),
        }
    }
}

/// Model of `with_borrowed_reader`.
///
/// Structurally identical to production: reconstruct the handle, wrap it in
/// [`ManuallyDrop`], run the operation. The `?` is the modelled unwind edge —
/// it leaves this frame before any trailing statement could run, so the
/// wrapper is the only thing standing between the reader and a close.
pub(crate) fn model_with_borrowed_reader<'log, T>(
    fd: ModelFd<'log>,
    operation: impl FnOnce(&mut ModelFd<'log>) -> Result<T, ModelUnwind>,
) -> Result<T, ModelUnwind> {
    let mut handle = ManuallyDrop::new(fd);
    let value = operation(&mut handle)?;
    Ok(value)
}

/// Model of `pump_stream`'s descriptor handling.
///
/// The writer is reconstructed as an owning handle and must close on every
/// exit path to signal EOF downstream; the reader is borrowed through the
/// helper and must survive every exit path.
pub(crate) fn model_pump_stream(
    reader: ModelFd<'_>,
    writer: ModelFd<'_>,
    exit: ExitMode,
) -> Result<(), ModelUnwind> {
    // Held to the end of the scope so it drops on the normal return and on
    // the early exit alike, exactly as the real writer handle does.
    let _writer_handle = writer;
    model_with_borrowed_reader(reader, |_reader_handle| exit.outcome())
}

/// Model of `consume_stream`'s descriptor handling.
///
/// `consume_stream` takes no writer at all: the reader is its only
/// descriptor, and it is borrowed.
pub(crate) fn model_consume_stream(reader: ModelFd<'_>, exit: ExitMode) -> Result<(), ModelUnwind> {
    model_with_borrowed_reader(reader, |_reader_handle| exit.outcome())
}

#[cfg(test)]
mod tests {
    //! Concrete unit tests for the ownership model, mirroring the bounded
    //! Kani proofs so the contract is exercised by `make test` as well.

    use super::{CloseLog, ExitMode, ModelFd, model_consume_stream, model_pump_stream};
    use rstest::rstest;

    #[rstest]
    #[case(ExitMode::Normal)]
    #[case(ExitMode::Unwind)]
    fn pump_borrows_reader_and_consumes_writer(#[case] exit: ExitMode) {
        let reader_log = CloseLog::new();
        let writer_log = CloseLog::new();

        let outcome = model_pump_stream(ModelFd::new(&reader_log), ModelFd::new(&writer_log), exit);

        assert_eq!(reader_log.closes(), 0, "borrowed reader must stay open");
        assert_eq!(
            writer_log.closes(),
            1,
            "writer must be consumed exactly once"
        );
        assert_eq!(outcome.is_err(), exit == ExitMode::Unwind);
    }

    #[rstest]
    #[case(ExitMode::Normal)]
    #[case(ExitMode::Unwind)]
    fn consume_borrows_its_only_reader(#[case] exit: ExitMode) {
        let reader_log = CloseLog::new();

        let outcome = model_consume_stream(ModelFd::new(&reader_log), exit);

        assert_eq!(reader_log.closes(), 0, "borrowed reader must stay open");
        assert_eq!(outcome.is_err(), exit == ExitMode::Unwind);
    }

    #[test]
    fn dropping_an_owning_handle_records_a_close() {
        // Non-vacuity: the model does report closes when nothing suppresses
        // the drop, so the zero-close assertions above are meaningful.
        let log = CloseLog::new();
        drop(ModelFd::new(&log));
        assert_eq!(log.closes(), 1);
    }
}
