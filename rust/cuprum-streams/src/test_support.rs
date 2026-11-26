//! Shared Unix file-descriptor fixtures and assertions for Rust unit tests.

use std::fmt::Debug;
#[cfg(not(miri))]
use std::{
    io::{self, Read, Write},
    os::fd::OwnedFd,
    sync::mpsc::{self, RecvTimeoutError},
    thread,
    time::Duration,
};

#[cfg(not(miri))]
use cap_std::fs::File;

/// Create an anonymous pipe as `(read_end, write_end)`.
///
/// Returns the operating-system error from `pipe(2)` so fixtures can pass it
/// to the test body, where the failure receives assertion context.
#[cfg(not(miri))]
pub(crate) fn make_pipe() -> io::Result<(OwnedFd, OwnedFd)> { cuprum_native_io::pipe() }

/// Duplicate a typed descriptor into an independently owned [`File`].
///
/// The caller retains its descriptor; cloning failures propagate to the test.
#[cfg(not(miri))]
pub(crate) fn dup_as_file(fd: &OwnedFd) -> io::Result<File> { fd.try_clone().map(File::from) }

/// Write every byte of `payload` through a duplicated descriptor.
///
/// This preserves failures from both `dup(2)` and [`Write::write_all`] for the
/// test body to assert on.
#[cfg(not(miri))]
pub(crate) fn write_all_to(fd: &OwnedFd, payload: &[u8]) -> io::Result<()> {
    let mut file = dup_as_file(fd)?;
    file.write_all(payload)
}

/// Read to EOF through a duplicated descriptor.
///
/// This preserves failures from both `dup(2)` and [`Read::read_to_end`] for
/// the test body to assert on.
#[cfg(not(miri))]
pub(crate) fn read_all_from(fd: &OwnedFd) -> io::Result<Vec<u8>> {
    let mut collected = Vec::new();
    let mut file = dup_as_file(fd)?;
    file.read_to_end(&mut collected)?;
    Ok(collected)
}

/// Why a bounded end-of-file observation produced no bytes.
#[cfg(not(miri))]
#[derive(Debug)]
pub(crate) enum EofReadError {
    /// The duplicated descriptor could not be read.
    Io(io::Error),
    /// The reader had not reached EOF when the caller's deadline expired.
    TimedOut(Duration),
    /// The worker ended without reporting a result.
    Disconnected,
}

#[cfg(not(miri))]
impl std::fmt::Display for EofReadError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "reading the pipe to EOF failed: {error}"),
            Self::TimedOut(deadline) => write!(
                formatter,
                "the pipe did not reach EOF within {deadline:?}; a writer duplicate is still open",
            ),
            Self::Disconnected => formatter.write_str("the EOF worker ended without a result"),
        }
    }
}

/// Read a pipe to EOF through a duplicate, bounded by `deadline` on the caller.
///
/// The read runs on a worker thread because `read_to_end` has no timeout of its
/// own: a writer that never closes would otherwise hold the test thread until
/// the whole suite timed out. The deadline therefore bounds the *caller*, not
/// the worker — a timed-out worker is left to finish on its own, which costs
/// one blocked thread until the process exits.
///
/// EOF is evidence of closure only when every write duplicate is closed: a
/// pipe reports end of input once its last writer drops, wherever that owner
/// lives. Callers that still hold a writer will see [`EofReadError::TimedOut`].
#[cfg(not(miri))]
pub(crate) fn read_to_eof_bounded(
    fd: &OwnedFd,
    deadline: Duration,
) -> Result<Vec<u8>, EofReadError> {
    let mut file = dup_as_file(fd).map_err(EofReadError::Io)?;
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut collected = Vec::new();
        let outcome = file.read_to_end(&mut collected).map(|_| collected);
        // A failed send means the caller already gave up on this worker.
        drop(sender.send(outcome));
    });

    match receiver.recv_timeout(deadline) {
        Ok(Ok(bytes)) => Ok(bytes),
        Ok(Err(error)) => Err(EofReadError::Io(error)),
        Err(RecvTimeoutError::Timeout) => Err(EofReadError::TimedOut(deadline)),
        Err(RecvTimeoutError::Disconnected) => Err(EofReadError::Disconnected),
    }
}

/// Extract a successful fixture result at a recognized test boundary.
pub(crate) fn unwrap_ok<T, E: Debug>(result: Result<T, E>) -> T {
    match result {
        Ok(value) => value,
        Err(err) => panic!("expected Ok(..), got Err({err:?})"),
    }
}

/// Extract an expected failure while retaining the successful value in panic
/// output.
pub(crate) fn unwrap_err<T: Debug, E>(result: Result<T, E>) -> E {
    match result {
        Ok(value) => panic!("expected Err(..), got Ok({value:?})"),
        Err(err) => err,
    }
}

#[cfg(all(test, not(miri)))]
#[path = "test_support_tests.rs"]
mod tests;
