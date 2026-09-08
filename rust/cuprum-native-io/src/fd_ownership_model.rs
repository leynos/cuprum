//! Close-effect model instantiated with the production retained-owner kernel.
//!
//! `ModelFd` substitutes an observable drop counter for an OS descriptor.
//! The production helper executes unchanged; only the resource is modelled.
//! Normal and error returns are checked here. Neither OS close effects nor
//! real panic-unwind are proved by this model: native regressions exercise
//! those paths, including the historical trailing-`mem::forget` mistake.

use crate::memory::with_retained_owner;
use core::cell::Cell;

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

/// Represents an ordinary operation error, not panic-unwind.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct ModelError;

/// How a modelled operation leaves its scope.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ExitMode {
    /// The operation ran to completion and returned normally.
    Normal,
    /// The operation returned an error.
    Error,
}

impl ExitMode {
    /// Outcome an operation returns under this exit mode.
    pub(crate) const fn outcome(self) -> Result<(), ModelError> {
        match self {
            Self::Normal => Ok(()),
            Self::Error => Err(ModelError),
        }
    }
}

/// Run the production retained-owner kernel with an observable resource.
pub(crate) fn model_with_borrowed_reader<'log, T>(
    fd: ModelFd<'log>,
    operation: impl FnOnce(&mut ModelFd<'log>) -> Result<T, ModelError>,
) -> Result<T, ModelError> {
    with_retained_owner(fd, operation)
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
) -> Result<(), ModelError> {
    // Held to the end of the scope so it drops on the normal return and on
    // the early exit alike, exactly as the real writer handle does.
    let _writer_handle = writer;
    model_with_borrowed_reader(reader, |_reader_handle| exit.outcome())
}

/// Model of `consume_stream`'s descriptor handling.
///
/// `consume_stream` takes no writer at all: the reader is its only
/// descriptor, and it is borrowed.
pub(crate) fn model_consume_stream(reader: ModelFd<'_>, exit: ExitMode) -> Result<(), ModelError> {
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
    #[case(ExitMode::Error)]
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
        assert_eq!(outcome.is_err(), exit == ExitMode::Error);
    }

    #[rstest]
    #[case(ExitMode::Normal)]
    #[case(ExitMode::Error)]
    fn consume_borrows_its_only_reader(#[case] exit: ExitMode) {
        let reader_log = CloseLog::new();

        let outcome = model_consume_stream(ModelFd::new(&reader_log), exit);

        assert_eq!(reader_log.closes(), 0, "borrowed reader must stay open");
        assert_eq!(outcome.is_err(), exit == ExitMode::Error);
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
