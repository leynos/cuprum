//! Prerequisite cases for the steps, called out of order.

use crate::unix_tests::{StreamContext, steps};

#[test]
fn pump_step_names_the_missing_reader() {
    let context = StreamContext::default();

    let error = steps::the_public_pump_runs(&context, 8)
        .expect_err("the pump step needs a reader and a writer");

    assert!(
        error.contains("no sink writer was available"),
        "the failure must name the missing writer, found {error}",
    );
}

#[test]
fn consume_step_names_the_missing_reader() {
    let context = StreamContext::default();

    let error =
        steps::the_public_consume_runs(&context, 8).expect_err("the consume step needs a reader");

    assert!(
        error.contains("no borrowed reader was recorded"),
        "the failure must name the missing reader, found {error}",
    );
}

#[test]
fn unwind_step_names_the_missing_reader() {
    let context = StreamContext::default();

    let error = steps::the_public_pump_unwinds(&context)
        .expect_err("the unwind step needs a reader and a writer");

    assert!(
        error.contains("no sink writer was available"),
        "the failure must name the missing writer, found {error}",
    );
}

#[test]
fn drain_step_names_the_missing_reader() {
    let context = StreamContext::default();

    let error = steps::the_source_is_drained_to_end_of_input(&context)
        .expect_err("the drain step needs a reader");

    assert!(
        error.contains("no borrowed reader was recorded"),
        "the failure must name the missing reader, found {error}",
    );
}

#[test]
fn writer_step_names_the_missing_sink_reader() {
    let context = StreamContext::default();

    let error = steps::the_pump_closed_its_writer(&context, "nothing".to_owned())
        .expect_err("the writer step needs a sink reader");

    assert!(
        error.contains("no sink reader was recorded"),
        "the failure must name the missing sink, found {error}",
    );
}
