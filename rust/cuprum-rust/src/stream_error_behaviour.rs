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

use cuprum_streams::PumpError;
use rstest::fixture;
use rstest_bdd::Slot;
use rstest_bdd_macros::{given, scenario, then, when};

use crate::{RustStreamError, convert_fd, validate_buffer_size};

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

// `fn_single_line` in rustfmt 1.9.0-nightly turns this rstest fixture into a
// form that triggers `unused_braces` under Rust 1.85. Remove this skip when
// that formatter/rstest combination compiles the configured profile cleanly.
#[rustfmt::skip]
#[fixture]
fn context() -> StreamErrorContext {
    StreamErrorContext::default()
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
fn the_buffer_validator_checks(context: &StreamErrorContext) {
    let size = context.buffer_size.get().expect("a buffer size was set");
    let observed = match validate_buffer_size(size) {
        Ok(_) => ObservedError::InvalidBufferSize("accepted".to_owned()),
        Err(error) => observe(error),
    };
    context.error.set(observed);
}

#[when("the native descriptor validator checks the value")]
fn the_descriptor_validator_checks(context: &StreamErrorContext) {
    let value = context.descriptor.get().expect("a descriptor was set");
    let observed = match convert_fd(value) {
        Ok(_) => ObservedError::InvalidDescriptor("accepted".to_owned()),
        Err(error) => observe(error),
    };
    context.error.set(observed);
}

#[when("it becomes a RustStreamError")]
fn it_becomes_a_rust_stream_error(context: &StreamErrorContext) {
    // The `Given` steps above already performed the production conversion and
    // recorded it. This step exists so the scenario reads as the behaviour it
    // describes, and asserts the recording actually happened rather than
    // silently leaving the slot empty.
    assert!(
        context.error.get().is_some(),
        "the conversion must have been performed before this step",
    );
}

#[then("the error is InvalidBufferSize")]
fn the_error_is_invalid_buffer_size(context: &StreamErrorContext) {
    let observed = context.error.get().expect("an error was recorded");
    assert!(
        matches!(observed, ObservedError::InvalidBufferSize(_)),
        "expected a buffer-size failure, found {observed:?}",
    );
}

#[then("the error is InvalidDescriptor")]
fn the_error_is_invalid_descriptor(context: &StreamErrorContext) {
    let observed = context.error.get().expect("an error was recorded");
    assert!(
        matches!(observed, ObservedError::InvalidDescriptor(_)),
        "expected a descriptor failure, found {observed:?}",
    );
}

#[then("the error is Stream")]
fn the_error_is_stream(context: &StreamErrorContext) {
    let observed = context.error.get().expect("an error was recorded");
    assert!(
        matches!(observed, ObservedError::Stream { .. }),
        "expected a stream failure, found {observed:?}",
    );
}

#[then("its message is {expected}")]
fn its_message_is(context: &StreamErrorContext, expected: String) {
    let observed = context.error.get().expect("an error was recorded");
    let message = match observed {
        ObservedError::InvalidBufferSize(message) | ObservedError::InvalidDescriptor(message) => {
            message
        }
        other @ ObservedError::Stream { .. } => {
            panic!("expected an argument failure, found {other:?}")
        }
    };
    assert_eq!(
        message, expected,
        "the message must be the stable contract text"
    );
}

#[then("the size is accepted as {size}")]
fn the_size_is_accepted(context: &StreamErrorContext, size: isize) {
    // The `When` step records an "accepted" marker for a valid size, so an
    // accepted outline row is distinguished from a rejection without needing
    // a second slot.
    let observed = context.error.get().expect("an outcome was recorded");
    assert_eq!(
        observed,
        ObservedError::InvalidBufferSize("accepted".to_owned()),
        "buffer size {size} must be accepted",
    );
}

#[then("the stream error retains platform error code {code}")]
fn the_stream_error_retains_platform_error_code(context: &StreamErrorContext, code: i32) {
    let observed = context.error.get().expect("an error was recorded");
    let ObservedError::Stream {
        code: Some(actual), ..
    } = observed
    else {
        panic!("a stream failure carrying an OS code was expected, found {observed:?}");
    };
    assert_eq!(
        actual, code,
        "the wrapped source must still report the code the syscall returned",
    );
}

#[then("the stream error retains the semantic message {expected}")]
fn the_stream_error_retains_the_semantic_message(context: &StreamErrorContext, expected: String) {
    let observed = context.error.get().expect("an error was recorded");
    let ObservedError::Stream { message, .. } = observed else {
        panic!("a stream failure was expected, found {observed:?}");
    };
    assert_eq!(
        message, expected,
        "the wrapped source must still render its stable message",
    );
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

#[scenario(path = "tests/features/stream_errors.feature")]
fn typed_native_stream_failures(context: StreamErrorContext) {
    // `rstest-bdd` injects the fixture; the body runs after every step, so
    // the scenario itself has nothing left to assert.
    let _ = context;
}

#[scenario(
    path = "tests/features/stream_errors.feature",
    name = "Accept a valid buffer size"
)]
fn accepts_a_valid_buffer_size(context: StreamErrorContext, size: isize) {
    let _ = (context, size);
}
