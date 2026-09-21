//! Loom-only bridge to the production pump transition machine.
//!
//! The lifecycle model belongs to the Python integration crate, but its I/O
//! outcomes must drive the same private state machine as production pumping.

use crate::pump_machine::{PumpState, WriteEvent, advance};

/// Exercise the production transition sequence for successful native I/O.
pub fn drive_successful_pump() {
    let mut pump = PumpState::start();
    let outcome = advance(&mut pump, 1, || {
        Ok::<WriteEvent, ()>(WriteEvent::Complete { bytes: 1 })
    });
    assert!(outcome.is_ok(), "a completed write must advance the pump");
    assert_eq!(
        pump.total_written(),
        1,
        "the completed write must add bytes"
    );
    assert!(
        pump.writer_open(),
        "a completed write keeps the writer open"
    );
    let eof = advance(&mut pump, 0, || -> Result<WriteEvent, ()> {
        unreachable!("EOF never writes")
    });
    assert!(eof.is_ok(), "EOF must stop the pump without a write");
    assert_eq!(pump.total_written(), 1, "EOF must not add bytes");
    assert!(pump.writer_open(), "EOF must not close the writer");
}

/// Exercise the production transition sequence for a downstream close.
pub fn drive_downstream_close() {
    let mut pump = PumpState::start();
    let outcome = advance(&mut pump, 1, || {
        Ok::<WriteEvent, ()>(WriteEvent::Closed { bytes: 0 })
    });
    assert!(outcome.is_ok(), "a downstream close is a non-fatal outcome");
    assert_eq!(pump.total_written(), 0, "a closed writer accepts no bytes");
    assert!(
        !pump.writer_open(),
        "downstream close must latch the writer shut"
    );
    let drain = advance(&mut pump, 1, || -> Result<WriteEvent, ()> {
        unreachable!("closed writer drains")
    });
    assert!(drain.is_ok(), "closed writers must drain later chunks");
    assert_eq!(pump.total_written(), 0, "draining must not add bytes");
    assert!(!pump.writer_open(), "draining must not reopen the writer");
    let eof = advance(&mut pump, 0, || -> Result<WriteEvent, ()> {
        unreachable!("EOF never writes")
    });
    assert!(eof.is_ok(), "EOF after a close must stay non-fatal");
    assert_eq!(pump.total_written(), 0, "EOF must not add drained bytes");
    assert!(!pump.writer_open(), "EOF must not reopen the writer");
}

/// Exercise the production transition sequence for a terminal native failure.
pub fn drive_failed_pump() {
    let mut pump = PumpState::start();
    let outcome = advance(&mut pump, 1, || Err::<WriteEvent, ()>(()));
    assert!(outcome.is_err(), "a native failure must propagate");
    assert_eq!(pump.total_written(), 0, "a failed write must not add bytes");
    assert!(
        pump.writer_open(),
        "a failed write must not latch the writer shut"
    );
}
