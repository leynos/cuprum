//! Actual resource effects and unwind regressions for the ownership boundary.

#[cfg(unix)]
use std::os::fd::{AsRawFd, IntoRawFd};
#[cfg(windows)]
use std::os::windows::io::{AsRawHandle, IntoRawHandle};
use std::panic::{AssertUnwindSafe, catch_unwind};

use rstest::{fixture, rstest};
use std::sync::{LockResult, Mutex, MutexGuard};

use super::{adopt_writer, borrow, borrow_reader, fd_is_open, pipe, read_once, with_owned_writer};

// Closed-descriptor observations must not race this test binary's next pipe.
// The guard is outside catch_unwind, so intentional panics do not poison it.
static DESCRIPTOR_TESTS: Mutex<()> = Mutex::new(());

#[fixture]
fn descriptor_guard() -> LockResult<MutexGuard<'static, ()>> {
    DESCRIPTOR_TESTS.lock()
}

#[rstest]
#[case::normal(false)]
#[case::real_unwind(true)]
fn borrowed_reader_survives(
    #[from(descriptor_guard)] guard_result: LockResult<MutexGuard<'static, ()>>,
    #[case] should_panic: bool,
) {
    let guard = guard_result.unwrap_or_else(|error| panic!("descriptor test lock: {error:?}"));
    let (reader, writer) = pipe().unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    let raw = raw_of(&reader);
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: reader remains owned outside this scope, including unwind.
        let view = unsafe { borrow_reader(raw) };
        // Windows also reconstructs a temporary capability File for I/O.
        // Unwind through that exact adapter before observing the real handle.
        #[cfg(windows)]
        if should_panic {
            drop(super::windows::with_file(view, |_| {
                panic!("injected real unwind")
            }));
        }
        assert!(!should_panic, "injected real unwind");
        let written = super::write_once(borrow(&writer), b"ping")
            .unwrap_or_else(|error| panic!("write payload: {error:?}"));
        assert_eq!(written, 4);
        drop(writer);
        let mut buffer = [0; 8];
        let count = read_once(view, &mut buffer)
            .unwrap_or_else(|error| panic!("read through borrow: {error:?}"));
        assert_eq!(count, 4);
        assert_eq!(buffer.get(..4), Some(b"ping".as_slice()));
    }));
    assert_eq!(outcome.is_err(), should_panic);
    assert!(fd_is_open(raw), "borrow must not close the caller's reader");
    drop(reader);
    assert!(
        !fd_is_open(raw),
        "the actual owner still closes the descriptor"
    );
    drop(guard);
}

#[rstest]
#[case::normal(0)]
#[case::error(1)]
#[case::real_unwind(2)]
fn transferred_writer_closes_and_delivers_eof(
    #[from(descriptor_guard)] guard_result: LockResult<MutexGuard<'static, ()>>,
    #[case] exit: u8,
) {
    let guard = guard_result.unwrap_or_else(|error| panic!("descriptor test lock: {error:?}"));
    let (reader, writer) = pipe().unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    let raw = into_raw(writer);
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: into_raw_fd relinquished the only owner immediately above.
        let owned = unsafe { adopt_writer(raw) };
        with_owned_writer(&reader, owned, |source, sink| {
            assert_eq!(
                super::write_once(borrow(sink), b"pong")
                    .unwrap_or_else(|error| panic!("write: {error:?}")),
                4
            );
            assert!(fd_is_open(raw_of(source)));
            match exit {
                2 => panic!("injected real unwind"),
                1 => Err("injected operation error"),
                _ => Ok(()),
            }
        })
    }));
    assert_eq!(outcome.is_err(), exit == 2);
    if exit == 1 {
        assert_eq!(
            outcome.unwrap_or_else(|error| panic!("no panic: {error:?}")),
            Err("injected operation error")
        );
    }
    assert!(
        !fd_is_open(raw),
        "transferred writer must close on every exit"
    );
    let mut bytes = [0; 8];
    assert_eq!(
        read_once(borrow(&reader), &mut bytes)
            .unwrap_or_else(|error| panic!("read payload: {error:?}")),
        4
    );
    assert_eq!(bytes.get(..4), Some(b"pong".as_slice()));
    assert_eq!(
        read_once(borrow(&reader), &mut bytes)
            .unwrap_or_else(|error| panic!("read EOF: {error:?}")),
        0
    );
    // A subsequent resource must not be closed by delayed ownership cleanup.
    let (replacement, _replacement_writer) =
        pipe().unwrap_or_else(|error| panic!("replacement pipe: {error:?}"));
    assert!(fd_is_open(raw_of(&replacement)));
    drop(guard);
}

#[rstest]
fn owned_descriptor_reads_and_closes(
    #[from(descriptor_guard)] guard_result: LockResult<MutexGuard<'static, ()>>,
) {
    let guard = guard_result.unwrap_or_else(|error| panic!("descriptor test lock: {error:?}"));
    let (reader, writer) = pipe().unwrap_or_else(|error| panic!("create pipe: {error:?}"));
    assert_eq!(
        super::write_once(borrow(&writer), b"pong")
            .unwrap_or_else(|error| panic!("write: {error:?}")),
        4
    );
    drop(writer);
    let raw = into_raw(reader);
    // SAFETY: the sole owner was consumed immediately above.
    let owned = unsafe { adopt_writer(raw) };
    let mut bytes = [0; 8];
    assert_eq!(
        read_once(borrow(&owned), &mut bytes).unwrap_or_else(|error| panic!("read: {error:?}")),
        4
    );
    assert_eq!(bytes.get(..4), Some(b"pong".as_slice()));
    drop(owned);
    assert!(!fd_is_open(raw));
    drop(guard);
}

fn raw_of(resource: &super::OwnedStream) -> super::PlatformFd {
    #[cfg(unix)]
    {
        resource.as_raw_fd()
    }
    #[cfg(windows)]
    {
        resource.as_raw_handle() as usize
    }
}

fn into_raw(resource: super::OwnedStream) -> super::PlatformFd {
    #[cfg(unix)]
    {
        resource.into_raw_fd()
    }
    #[cfg(windows)]
    {
        resource.into_raw_handle() as usize
    }
}
