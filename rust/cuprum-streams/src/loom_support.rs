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
    assert!(outcome.is_ok());
    let eof = advance(&mut pump, 0, || -> Result<WriteEvent, ()> {
        unreachable!("EOF never writes")
    });
    assert!(eof.is_ok());
}

/// Exercise the production transition sequence for a downstream close.
pub fn drive_downstream_close() {
    let mut pump = PumpState::start();
    let outcome = advance(&mut pump, 1, || {
        Ok::<WriteEvent, ()>(WriteEvent::Closed { bytes: 0 })
    });
    assert!(outcome.is_ok());
    let drain = advance(&mut pump, 1, || -> Result<WriteEvent, ()> {
        unreachable!("closed writer drains")
    });
    assert!(drain.is_ok());
    let eof = advance(&mut pump, 0, || -> Result<WriteEvent, ()> {
        unreachable!("EOF never writes")
    });
    assert!(eof.is_ok());
}

/// Exercise the production transition sequence for a terminal native failure.
pub fn drive_failed_pump() {
    let mut pump = PumpState::start();
    let outcome = advance(&mut pump, 1, || Err::<WriteEvent, ()>(()));
    assert!(outcome.is_err());
}
