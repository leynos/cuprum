//! Miri-compatible tests for the native writer ownership scope.

use std::cell::Cell;

struct InMemoryWriter<'a>(&'a Cell<u8>);

impl Drop for InMemoryWriter<'_> {
    fn drop(&mut self) { self.0.set(self.0.get().saturating_add(1)); }
}

/// The native ownership scope drops an in-memory writer after normal return.
#[test]
fn with_owned_writer_drops_an_in_memory_writer_after_normal_exit() {
    let drops = Cell::new(0);
    let outcome = cuprum_native_io::with_owned_writer(&(), InMemoryWriter(&drops), |(), _| {
        assert_eq!(
            drops.get(),
            0,
            "the writer remains live during the operation"
        );
        "completed"
    });

    assert_eq!(outcome, "completed");
    assert_eq!(drops.get(), 1, "the writer drops once after normal exit");
}

/// The native ownership scope drops an in-memory writer after an error return.
#[test]
fn with_owned_writer_drops_an_in_memory_writer_after_error_exit() {
    let drops = Cell::new(0);
    let outcome: Result<(), &str> =
        cuprum_native_io::with_owned_writer(&(), InMemoryWriter(&drops), |(), _| {
            assert_eq!(
                drops.get(),
                0,
                "the writer remains live during the operation"
            );
            Err("operation failed")
        });

    assert_eq!(outcome, Err("operation failed"));
    assert_eq!(drops.get(), 1, "the writer drops once after an error exit");
}
