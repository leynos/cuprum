//! Loom-only bridge to the audited borrowed-reader ownership model.
//!
//! The public Loom harness lives in the Python integration crate, while the
//! retained-owner contract remains private to this audited boundary crate.
//! This bridge executes that contract with observable model descriptors.

use crate::fd_ownership_model::{
    CloseLog, ExitMode, ModelFd, model_consume_stream, model_pump_stream,
};

/// Execute the borrowed-reader ownership cases and return its close count.
#[must_use]
pub fn borrowed_reader_close_count() -> usize {
    let reader_log = CloseLog::new();
    for exit in [ExitMode::Normal, ExitMode::Error] {
        let writer_log = CloseLog::new();
        let outcome = model_pump_stream(ModelFd::new(&reader_log), ModelFd::new(&writer_log), exit);
        assert_eq!(outcome.is_ok(), matches!(exit, ExitMode::Normal));
        assert_eq!(writer_log.closes(), 1);
    }
    let consume_log = CloseLog::new();
    assert!(model_consume_stream(ModelFd::new(&consume_log), ExitMode::Normal).is_ok());
    assert_eq!(consume_log.closes(), 0);
    match usize::try_from(reader_log.closes()) {
        Ok(closes) => closes,
        Err(_) => unreachable!("u32 close count must fit into usize"),
    }
}
