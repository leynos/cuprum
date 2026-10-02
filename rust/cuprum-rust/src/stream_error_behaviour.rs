//! Behavioural scenarios for the typed native stream boundary.
//!
//! These run the production validators and conversions through
//! `rstest-bdd` scenarios loaded from `tests/features/stream_errors.feature`.
//! They live inside the crate rather than under `tests/` so the steps can
//! reach the crate-private [`RustStreamError`] without widening its
//! visibility — the enum is an internal boundary, and exporting it to make a
//! test reachable would be the tail wagging the dog.
//!
//! The steps call the real validators and the real `From` conversion, so the
//! classification they assert is the one production callers receive. Nothing
//! here inspects a `PyErr`: this crate links no interpreter, so the Python
//! half of the contract is asserted by the extension-required Python suite.
//!
//! Every step is fallible and returns a `StepResult`. `rstest-bdd` erases the
//! step attributes while expanding, so these functions carry no test marker by
//! the time the house lint sees them, and a `.expect(...)` here would read as
//! production code. Returning a step error also names the step that failed,
//! where an `expect` reports only the slot access that came up empty.
//!
//! Every step is also reachable from a bound scenario: an unreachable step
//! would compile, register, and never run, which is how this module first lost
//! three of its five scenarios.

use cuprum_streams::PumpError;
use rstest::fixture;
use rstest_bdd::{Slot, StepResult};
use rstest_bdd_macros::{given, scenario, then, when};

use crate::{RustStreamError, convert_fd, validate_buffer_size};

/// The marker the validator steps record when they accept an argument.
const ACCEPTED: &str = "accepted";

/// Per-scenario state, shared between steps through `rstest-bdd`'s slots.
#[derive(Default)]
struct StreamErrorContext {
    /// The buffer size under test.
    buffer_size: Slot<i64>,
    /// The descriptor value under test.
    descriptor: Slot<i64>,
    /// The last boundary error observed, rendered for comparison.
    error: Slot<ObservedError>,
}

/// The observable shape of a boundary failure.
///
/// Storing this rather than the error itself keeps the variants comparable
/// across steps without exposing a `PyErr`.
#[derive(Clone, Debug, PartialEq, Eq)]
enum ObservedError {
    /// The refusal classified the argument as a buffer size.
    InvalidBufferSize(String),
    /// The refusal classified the argument as a descriptor.
    InvalidDescriptor(String),
    /// The engine failed, carrying whether it kept a platform error code.
    Stream {
        /// The raw platform code the wrapped source still reports.
        code: Option<i32>,
        /// The wrapped source's display message.
        message: String,
    },
}

#[fixture]
fn context() -> StreamErrorContext {
    // A fresh context per scenario; steps populate its slots as they run.
    StreamErrorContext::default()
}

/// Read a scenario value that an earlier step was required to record.
///
/// Every step that reads shared state goes through this accessor, so a
/// scenario whose steps ran out of order fails with the name of the value that
/// was missing rather than with a bare unwrap.
fn recorded<T: Clone>(slot: &Slot<T>, label: &str) -> StepResult<T, String> {
    slot.get()
        .ok_or_else(|| format!("no {label} was recorded; a Given step must run first"))
}

#[given("a buffer size of {size}")]
fn a_buffer_size_of(context: &StreamErrorContext, size: isize) {
    context.buffer_size.set(size as i64);
}

#[given("a descriptor value of {value}")]
fn a_descriptor_value_of(context: &StreamErrorContext, value: isize) {
    context.descriptor.set(value as i64);
}

#[given("a stream I/O error with platform error code {code}")]
fn a_stream_io_error_with_code(context: &StreamErrorContext, code: i32) {
    let source = std::io::Error::from_raw_os_error(code);
    context
        .error
        .set(observe(RustStreamError::from(PumpError::from(source))));
}

#[given("a semantic stream failure of BufferRangeExceeded")]
fn a_semantic_stream_failure(context: &StreamErrorContext) {
    context.error.set(observe(RustStreamError::from(
        PumpError::BufferRangeExceeded,
    )));
}

#[when("the native buffer validator checks the size")]
fn the_buffer_validator_checks(context: &StreamErrorContext) -> StepResult<(), String> {
    let size = recorded(&context.buffer_size, "buffer size")?;
    let observed = match validate_buffer_size(size) {
        Ok(_) => ObservedError::InvalidBufferSize(ACCEPTED.to_owned()),
        Err(error) => observe(error),
    };
    context.error.set(observed);
    Ok(())
}

#[when("the native descriptor validator checks the value")]
fn the_descriptor_validator_checks(context: &StreamErrorContext) -> StepResult<(), String> {
    let value = recorded(&context.descriptor, "descriptor value")?;
    let observed = match convert_fd(value) {
        Ok(_) => ObservedError::InvalidDescriptor(ACCEPTED.to_owned()),
        Err(error) => observe(error),
    };
    context.error.set(observed);
    Ok(())
}

#[when("it becomes a RustStreamError")]
fn it_becomes_a_rust_stream_error(context: &StreamErrorContext) -> StepResult<(), String> {
    // The `Given` steps above already performed the production conversion and
    // recorded it. This step exists so the scenario reads as the behaviour it
    // describes, and refuses when the recording never happened rather than
    // silently leaving the slot empty.
    recorded(&context.error, "converted error").map(|_| ())
}

#[then("the error is InvalidBufferSize")]
fn the_error_is_invalid_buffer_size(context: &StreamErrorContext) -> StepResult<(), String> {
    let observed = recorded(&context.error, "converted error")?;
    if !matches!(observed, ObservedError::InvalidBufferSize(_)) {
        return Err(format!(
            "expected a buffer-size failure, found {observed:?}"
        ));
    }
    Ok(())
}

#[then("the error is InvalidDescriptor")]
fn the_error_is_invalid_descriptor(context: &StreamErrorContext) -> StepResult<(), String> {
    let observed = recorded(&context.error, "converted error")?;
    if !matches!(observed, ObservedError::InvalidDescriptor(_)) {
        return Err(format!("expected a descriptor failure, found {observed:?}"));
    }
    Ok(())
}

#[then("the error is Stream")]
fn the_error_is_stream(context: &StreamErrorContext) -> StepResult<(), String> {
    let observed = recorded(&context.error, "converted error")?;
    if !matches!(observed, ObservedError::Stream { .. }) {
        return Err(format!("expected a stream failure, found {observed:?}"));
    }
    Ok(())
}

#[then("its message is {expected}")]
fn its_message_is(context: &StreamErrorContext, expected: String) -> StepResult<(), String> {
    let observed = recorded(&context.error, "converted error")?;
    let message = match observed {
        ObservedError::InvalidBufferSize(message) | ObservedError::InvalidDescriptor(message) => {
            message
        }
        other @ ObservedError::Stream { .. } => {
            return Err(format!("expected an argument failure, found {other:?}"));
        }
    };
    if message != expected {
        return Err(format!(
            "the message must be the stable contract text; expected {expected:?}, found \
             {message:?}"
        ));
    }
    Ok(())
}

#[then("the size is accepted as {size}")]
fn the_size_is_accepted(context: &StreamErrorContext, size: isize) -> StepResult<(), String> {
    // The `When` step records an "accepted" marker for a valid size, so an
    // accepted outline row is distinguished from a rejection without needing
    // a second slot.
    let observed = recorded(&context.error, "validator outcome")?;
    if observed != ObservedError::InvalidBufferSize(ACCEPTED.to_owned()) {
        return Err(format!(
            "buffer size {size} must be accepted, found {observed:?}"
        ));
    }
    Ok(())
}

#[then("the stream error retains platform error code {code}")]
fn the_stream_error_retains_platform_error_code(
    context: &StreamErrorContext,
    code: i32,
) -> StepResult<(), String> {
    let observed = recorded(&context.error, "converted error")?;
    let ObservedError::Stream {
        code: Some(actual), ..
    } = observed
    else {
        return Err(format!(
            "a stream failure carrying an OS code was expected, found {observed:?}"
        ));
    };
    if actual != code {
        return Err(format!(
            "the wrapped source must still report the code the syscall returned; expected {code}, \
             found {actual}"
        ));
    }
    Ok(())
}

#[then("the stream error retains the semantic message {expected}")]
fn the_stream_error_retains_the_semantic_message(
    context: &StreamErrorContext,
    expected: String,
) -> StepResult<(), String> {
    let observed = recorded(&context.error, "converted error")?;
    let ObservedError::Stream { message, .. } = observed else {
        return Err(format!("a stream failure was expected, found {observed:?}"));
    };
    if message != expected {
        return Err(format!(
            "the wrapped source must still render its stable message; expected {expected:?}, \
             found {message:?}"
        ));
    }
    Ok(())
}

/// Reduce a boundary error to its observable shape.
///
/// This is the only classifier in the module, and it reads the real variants
/// rather than re-deriving them, so a mutation that misroutes a variant in
/// production changes what these scenarios see.
fn observe(error: RustStreamError) -> ObservedError {
    match error {
        RustStreamError::InvalidBufferSize(message) => {
            ObservedError::InvalidBufferSize(message.to_owned())
        }
        RustStreamError::InvalidDescriptor(message) => {
            ObservedError::InvalidDescriptor(message.to_owned())
        }
        RustStreamError::Stream(source) => match source {
            PumpError::Io(inner) => ObservedError::Stream {
                code: inner.raw_os_error(),
                message: inner.to_string(),
            },
            other => ObservedError::Stream {
                code: None,
                message: other.to_string(),
            },
        },
    }
}

// One `#[scenario]` binds exactly one scenario, and a binding that omits both
// `name` and `index` silently takes the first in the file. Each scenario below
// is therefore bound by name: an unnamed binding would run "Reject an invalid
// buffer before stream preparation" three times over and leave the rest of the
// feature unexecuted, which looks like coverage while proving nothing.

#[scenario(
    path = "tests/features/stream_errors.feature",
    name = "Reject an invalid buffer before stream preparation"
)]
fn rejects_an_invalid_buffer(context: StreamErrorContext) {
    // `rstest-bdd` injects the fixture; the body runs after every step, so
    // the scenario itself has nothing left to assert.
    let _ = context;
}

#[scenario(
    path = "tests/features/stream_errors.feature",
    name = "Reject a buffer above the cap"
)]
fn rejects_a_buffer_above_the_cap(context: StreamErrorContext) { let _ = context; }

#[scenario(
    path = "tests/features/stream_errors.feature",
    name = "Accept a valid buffer size"
)]
fn accepts_a_valid_buffer_size(context: StreamErrorContext, size: isize) {
    // The outline's `size` column binds to this parameter; the same value
    // reaches the steps of each generated case.
    let _ = (context, size);
}

#[scenario(
    path = "tests/features/stream_errors.feature",
    name = "Reject a negative descriptor"
)]
fn rejects_a_negative_descriptor(context: StreamErrorContext) { let _ = context; }

#[scenario(
    path = "tests/features/stream_errors.feature",
    name = "Retain a native I/O failure"
)]
fn retains_a_native_io_failure(context: StreamErrorContext) { let _ = context; }

#[scenario(
    path = "tests/features/stream_errors.feature",
    name = "Retain a semantic stream failure"
)]
fn retains_a_semantic_stream_failure(context: StreamErrorContext) { let _ = context; }
