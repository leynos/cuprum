//! Loom-only bridge to the audited borrowed-reader ownership model.
//!
//! The public Loom harness lives in the Python integration crate, while the
//! retained-owner contract remains private to this audited boundary crate.
//! This bridge executes that contract with observable model descriptors.

use crate::fd_ownership_model::{
    CloseLog,
    ExitMode,
    ModelFd,
    model_consume_stream,
    model_pump_stream,
};

/// Native-pump exit selected by the Loom environment actor.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PumpExit {
    /// The synchronous pump returns normally.
    Succeeded,
    /// The synchronous pump returns a terminal error.
    Failed,
}

/// Observable ownership effects of one modelled native-pump invocation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PumpCloseCounts {
    /// Closes recorded for the borrowed reader.
    pub reader_closes: usize,
    /// Closes recorded for the worker-owned writer.
    pub writer_closes: usize,
}

/// Execute one native-pump ownership path and return both descriptor effects.
#[must_use]
pub fn pump_close_counts(pump_exit: PumpExit) -> PumpCloseCounts {
    let reader_log = CloseLog::new();
    let writer_log = CloseLog::new();
    let exit = match pump_exit {
        PumpExit::Succeeded => ExitMode::Normal,
        PumpExit::Failed => ExitMode::Error,
    };
    let outcome = model_pump_stream(ModelFd::new(&reader_log), ModelFd::new(&writer_log), exit);
    assert_eq!(outcome.is_ok(), matches!(pump_exit, PumpExit::Succeeded));
    let consume_log = CloseLog::new();
    assert!(model_consume_stream(ModelFd::new(&consume_log), ExitMode::Normal).is_ok());
    assert_eq!(consume_log.closes(), 0);

    PumpCloseCounts {
        reader_closes: close_count(reader_log),
        writer_closes: close_count(writer_log),
    }
}

fn close_count(log: CloseLog) -> usize {
    match usize::try_from(log.closes()) {
        Ok(closes) => closes,
        Err(_) => unreachable!("u32 close count must fit into usize"),
    }
}
