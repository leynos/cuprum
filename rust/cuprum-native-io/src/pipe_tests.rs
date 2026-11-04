//! Constructor semantics for the standard-library-backed pipe.
//!
//! These are real-resource tests: they exercise OS endpoints, so they are
//! excluded from Miri by the boundary target's selection rather than being
//! interpreted. Every blocking read runs on a spawned thread and returns
//! through a channel with a deadline, so a lost EOF fails the test instead of
//! hanging the suite.

#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(windows)]
use std::os::windows::io::{AsHandle, AsRawHandle};
use std::{
    io,
    sync::mpsc::{self, RecvTimeoutError},
    thread,
    time::Duration,
};

use rstest::{fixture, rstest};

use super::{BorrowedStream, OwnedStream, borrow, fd_is_open, pipe, read_once, write_once};

/// How long a spawned reader may take before the test fails.
///
/// The pipe is local and in-process, so a correct transfer completes in
/// microseconds. The margin exists only so a lost EOF or a deadlocked writer
/// reports as a failure rather than wedging the run.
const READ_DEADLINE: Duration = Duration::from_secs(10);

/// Owned endpoints created once per test, in reader-first order.
#[fixture]
fn endpoints() -> Result<(OwnedStream, OwnedStream), io::Error> {
    // `fn_single_line` in rustfmt 1.9.0-nightly collapses a lone-expression body
    // to `{ pipe() }`, which `unused_braces` rejects under the compiled profile.
    // This comment keeps the body multi-line; `descriptor_guard` in
    // `ownership_tests.rs` records the same constraint as a `#[rustfmt::skip]`.
    pipe()
}

/// Consume the reader on a worker thread and return everything it read.
///
/// A writer that is still open makes the read block; the caller turns that
/// into a deadline failure, so a forgotten writer can never hang the suite.
fn read_to_eof_on_worker(reader: OwnedStream) -> mpsc::Receiver<io::Result<Vec<u8>>> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        // A failed send means the test already went away with the receiver;
        // the worker has nothing left to report in that case.
        drop(sender.send(read_all(&reader)));
    });
    receiver
}

/// Receive a worker's payload, failing rather than hanging when it is late.
fn await_payload(receiver: &mpsc::Receiver<io::Result<Vec<u8>>>) -> Vec<u8> {
    match receiver.recv_timeout(READ_DEADLINE) {
        Ok(Ok(payload)) => payload,
        Ok(Err(error)) => panic!("worker read failed: {error:?}"),
        Err(RecvTimeoutError::Timeout) => {
            panic!("worker did not reach EOF within {READ_DEADLINE:?}; a writer is still open")
        }
        Err(RecvTimeoutError::Disconnected) => panic!("worker dropped its sender without a result"),
    }
}

/// Read to EOF in small chunks so a transfer needs several iterations.
fn read_all(reader: &OwnedStream) -> io::Result<Vec<u8>> {
    let mut collected = Vec::new();
    let mut buffer = [0_u8; 7];
    loop {
        let count = read_payload(borrow(reader), &mut buffer)?;
        if count == 0 {
            return Ok(collected);
        }
        let chunk = buffer.get(..count).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "pipe read exceeded its buffer")
        })?;
        collected.extend_from_slice(chunk);
    }
}

/// Convert a signed native count into a length, rejecting impossible values.
fn count_of(count: isize) -> io::Result<usize> {
    usize::try_from(count).map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

/// Read once through the platform's borrowed-reader primitive.
fn read_payload(stream: BorrowedStream<'_>, buffer: &mut [u8]) -> io::Result<usize> {
    #[cfg(unix)]
    {
        count_of(read_once(stream, buffer)?)
    }
    #[cfg(windows)]
    {
        // The endpoint came from `pipe`, whose Windows half `synchronous_pipe`
        // documents as `CreatePipe`-backed and therefore synchronous.
        // SAFETY: the process's own `pipe` constructor made this endpoint, so
        // it is not an overlapped handle.
        let synchronous = unsafe { super::SynchronousBorrowedStream::new_unchecked(stream) };
        count_of(read_once(synchronous, buffer)?)
    }
}

/// Write a payload through the platform's borrowed-writer primitive.
fn write_payload(stream: BorrowedStream<'_>, payload: &[u8]) -> io::Result<usize> {
    #[cfg(unix)]
    {
        count_of(write_once(stream, payload)?)
    }
    #[cfg(windows)]
    {
        // The endpoint came from `pipe`, whose Windows half `synchronous_pipe`
        // documents as `CreatePipe`-backed and therefore synchronous.
        // SAFETY: the process's own `pipe` constructor made this endpoint, so
        // it is not an overlapped handle.
        let synchronous = unsafe { super::SynchronousBorrowedStream::new_unchecked(stream) };
        count_of(write_once(synchronous, payload)?)
    }
}

/// A transferred payload arrives intact and the reader reaches EOF.
#[rstest]
fn payload_transfers_and_reader_reaches_eof(
    endpoints: Result<(OwnedStream, OwnedStream), io::Error>,
) {
    let (reader, writer) = endpoints.unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    let payload = b"cuprum pipes bytes";
    let receiver = read_to_eof_on_worker(reader);

    let written = write_payload(borrow(&writer), payload)
        .unwrap_or_else(|error| panic!("write payload: {error:?}"));
    assert_eq!(written, payload.len(), "the pipe accepts the whole payload");
    drop(writer);

    assert_eq!(await_payload(&receiver), payload);
}

/// EOF waits for the last writer owner, not the first one dropped.
#[rstest]
fn last_writer_owner_governs_eof(endpoints: Result<(OwnedStream, OwnedStream), io::Error>) {
    let (reader, writer) = endpoints.unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    let duplicate = writer
        .try_clone()
        .unwrap_or_else(|error| panic!("clone writer: {error:?}"));
    let receiver = read_to_eof_on_worker(reader);

    // Drop the original first: the duplicate still holds the write end open,
    // so the worker must not observe EOF yet.
    drop(writer);
    assert!(
        receiver.recv_timeout(Duration::from_millis(100)).is_err(),
        "a live duplicate writer must keep the read blocked",
    );

    let payload = b"written through the duplicate";
    let written = write_payload(borrow(&duplicate), payload)
        .unwrap_or_else(|error| panic!("write through duplicate: {error:?}"));
    assert_eq!(written, payload.len());
    drop(duplicate);

    assert_eq!(
        await_payload(&receiver),
        payload,
        "the full payload, then EOF, follows the last writer being dropped",
    );
}

/// An early reader close makes the writer fail without failing the process.
#[rstest]
fn early_reader_close_reports_broken_pipe(
    endpoints: Result<(OwnedStream, OwnedStream), io::Error>,
) {
    let (reader, writer) = endpoints.unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    let reader_raw = raw_of(&reader);
    drop(reader);
    assert!(
        !descriptor_is_open(reader_raw),
        "the early close took effect before the write",
    );

    // A payload larger than the platform's pipe capacity cannot be buffered by
    // a pipe with no reader, so the failure is deterministic rather than a
    // race against a small payload fitting in the buffer.
    let payload = vec![b'x'; 1 << 16];
    let error =
        write_payload(borrow(&writer), &payload).expect_err("a write with no reader must fail");
    assert_eq!(
        error.kind(),
        io::ErrorKind::BrokenPipe,
        "the native error reaches the caller unchanged",
    );
}

/// Both endpoints are created close-on-exec.
#[rstest]
#[cfg(unix)]
fn unix_endpoints_are_close_on_exec(endpoints: Result<(OwnedStream, OwnedStream), io::Error>) {
    let (reader, writer) = endpoints.unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    assert!(
        !exec_inheritable(raw_of(&reader)),
        "the reader must not survive an exec",
    );
    assert!(
        !exec_inheritable(raw_of(&writer)),
        "the writer must not survive an exec",
    );
}

/// Both endpoints are created non-inheritable and work synchronously.
#[rstest]
#[cfg(windows)]
fn windows_endpoints_are_non_inheritable_and_synchronous() {
    let (reader, writer) = pipe().unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    assert!(
        !inheritable(raw_of(&reader)),
        "the reader handle must not be inheritable",
    );
    assert!(
        !inheritable(raw_of(&writer)),
        "the writer handle must not be inheritable",
    );

    // The capability wrapper is only sound over handles the constructor claims
    // are synchronous; a round trip through the single-call primitives proves
    // that claim holds for these endpoints.
    let (synchronous_reader, synchronous_writer) = super::synchronous_pipe()
        .unwrap_or_else(|error| panic!("create synchronous pipe: {error:?}"));
    let payload = b"synchronous";
    let written = write_once(synchronous_writer.as_synchronous_borrowed(), payload)
        .unwrap_or_else(|error| panic!("synchronous write: {error:?}"));
    assert_eq!(
        count_of(written).unwrap_or_else(|error| panic!("bad write count: {error:?}")),
        payload.len()
    );
    drop(synchronous_writer);

    let mut buffer = [0_u8; 32];
    let count = read_once(synchronous_reader.as_synchronous_borrowed(), &mut buffer)
        .unwrap_or_else(|error| panic!("synchronous read: {error:?}"));
    assert_eq!(
        count_of(count).unwrap_or_else(|error| panic!("bad read count: {error:?}")),
        payload.len()
    );
    assert_eq!(buffer.get(..payload.len()), Some(payload.as_slice()));
}

/// Observe a descriptor, failing loudly when observation itself fails.
fn descriptor_is_open(raw: super::PlatformFd) -> bool {
    match fd_is_open(raw) {
        Ok(is_open) => is_open,
        Err(error) => panic!("descriptor observation failed: {error:?}"),
    }
}

/// Report a raw endpoint identity for the current platform.
fn raw_of(resource: &OwnedStream) -> super::PlatformFd {
    #[cfg(unix)]
    {
        resource.as_raw_fd()
    }
    #[cfg(windows)]
    {
        resource.as_handle().as_raw_handle() as usize
    }
}

/// Report whether a Unix descriptor inherits across `exec`.
#[cfg(unix)]
fn exec_inheritable(fd: super::PlatformFd) -> bool {
    super::fd_is_exec_inheritable(fd)
        .unwrap_or_else(|error| panic!("observe FD_CLOEXEC: {error:?}"))
}

/// Report whether a Windows handle inherits into a child process.
#[cfg(windows)]
fn inheritable(raw: super::PlatformFd) -> bool {
    super::handle_is_inheritable(raw)
        .unwrap_or_else(|error| panic!("observe HANDLE_FLAG_INHERIT: {error:?}"))
}
