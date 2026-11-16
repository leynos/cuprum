//! Check the compiled Python extension against the independent Python consumer.
//!
//! The oracle side is `tests/helpers/consume_boundary_oracle.py`, run through
//! the project interpreter; it builds its expected text with Cuprum's pure
//! Python `_consume_stream`, so a failure here means the native path disagrees
//! with the fallback rather than with a copy of itself.
//!
//! The whole file is `#[ignore]`d and runs only through `make
//! test-boundary-parity`, which selects the three ignored tests, runs them, and
//! fails unless the run reports exactly 30 tests. Ordinary `make test` does not
//! execute them: each case launches a child interpreter, and the extension has
//! to be built first. The count guard is what stops a renamed or dropped case
//! from silently shrinking the suite while still exiting zero.
//!
//! Generated cases come from `support/boundary_strategies.rs`, curated seeds
//! from `support/boundary_seeds.rs`, the child-process launcher from
//! `support/bounded_child.rs`, and the oracle invocation plus its provenance
//! checks from `support/boundary_child.rs`. That last split keeps this file to
//! the properties and named witnesses under test, which is what the plan's
//! per-file size limit is protecting.

use std::cell::RefCell;

use proptest::{
    prelude::*,
    test_runner::{Config, FileFailurePersistence, RngSeed, TestCaseError, TestRunner},
};
use rstest::rstest;

#[path = "support/boundary_child.rs"]
mod boundary_child;
#[path = "support/boundary_seeds.rs"]
mod boundary_seeds;
#[path = "support/boundary_strategies.rs"]
mod boundary_strategies;
#[path = "support/bounded_child.rs"]
mod bounded_child;

use boundary_child::{BoundaryCase, run_boundary_case};

const SMALL_CASES_PER_BUFFER_BAND: u32 = 64;
const LARGE_CASES_PER_BUFFER_SIZE: u32 = 4;
const SHRINK_TIME_MILLIS: u32 = 120_000;
const REGRESSION_EXTENSION: &str = "proptest-regressions";

struct PropertyRun {
    cases: u32,
    max_shrink_iters: u32,
    rng_seed: RngSeed,
    persist_failures: bool,
    label: String,
}

fn property_runner(run: &PropertyRun) -> TestRunner {
    let mut config = Config::with_source_file(file!());
    config.cases = run.cases;
    config.max_shrink_iters = run.max_shrink_iters;
    config.max_shrink_time = SHRINK_TIME_MILLIS;
    config.rng_seed = run.rng_seed;
    config.failure_persistence = if run.persist_failures {
        Some(Box::new(FileFailurePersistence::WithSource(
            REGRESSION_EXTENSION,
        )))
    } else {
        None
    };
    TestRunner::new(config)
}

fn run_parity_property<S>(strategy: S, run: &PropertyRun) -> Result<(), String>
where
    S: Strategy<Value = (boundary_strategies::Payload, u32)>,
{
    let mut runner = property_runner(run);
    let outcome = runner.run(&strategy, |(payload, buffer_size)| {
        run_boundary_case(&BoundaryCase::new(payload.as_bytes(), buffer_size))
            .map_err(TestCaseError::fail)
    });
    outcome.map_err(|error| format!("{} failed: {error}", run.label))?;
    Ok(())
}

fn replay_seed<S>(
    strategy: S,
    buffer_size: u32,
    seed: u64,
) -> Result<boundary_strategies::Payload, String>
where
    S: Strategy<Value = boundary_strategies::Payload>,
{
    let run = PropertyRun {
        cases: 1,
        max_shrink_iters: 1024,
        rng_seed: RngSeed::Fixed(seed),
        persist_failures: false,
        label: format!("seed {seed}"),
    };
    let mut runner = property_runner(&run);
    let payload = RefCell::new(None);
    let outcome = runner.run(&strategy, |generated| {
        payload.replace(Some(generated.clone()));
        run_boundary_case(&BoundaryCase::new(generated.as_bytes(), buffer_size))
            .map_err(TestCaseError::fail)
    });
    outcome.map_err(|error| format!("seed {seed} replay failed: {error}"))?;
    payload
        .into_inner()
        .ok_or_else(|| format!("seed {seed} replay executed no generated case"))
}

#[rstest]
#[case::empty(b"", 1)]
#[case::nul(b"\0", 1)]
#[case::ascii(b"ordinary ASCII", 8)]
#[case::valid_two_byte_scalar(b"\xc2\xa2", 2)]
#[case::valid_three_byte_scalar(b"\xe2\x98\x83", 3)]
#[case::valid_four_byte_scalar(b"\xf0\x9f\x8c\xb1", 4)]
#[case::lone_continuation(b"\x80", 1)]
#[case::overlong_encoding(b"\xc0\xaf", 2)]
#[case::surrogate_encoding(b"\xed\xa0\x80", 3)]
#[case::above_unicode_maximum(b"\xf4\x90\x80\x80", 4)]
#[case::truncated_at_eof(b"prefix-\xe2\x98", 8)]
#[ignore = "requires the compiled extension; run make test-boundary-parity"]
fn native_boundary_named_utf8_witnesses(#[case] payload: &[u8], #[case] buffer_size: u32) {
    run_boundary_case(&BoundaryCase::new(payload, buffer_size))
        .unwrap_or_else(|error| panic!("{error}"));
}

#[rstest]
#[case::buffer_1(1, 0)]
#[case::buffer_2(2, 1)]
#[case::buffer_3(3, 2)]
#[case::buffer_4(4, 3)]
#[ignore = "requires the compiled extension; run make test-boundary-parity"]
fn native_boundary_splits_four_byte_scalar_at_small_buffers(
    #[case] buffer_size: u32,
    #[case] prefix_length: usize,
) {
    let mut payload = vec![b'x'; prefix_length];
    payload.extend_from_slice("🌱".as_bytes());
    run_boundary_case(&BoundaryCase::new(&payload, buffer_size))
        .unwrap_or_else(|error| panic!("{error}"));
}

#[rstest]
#[case::buffer_65535(65_535)]
#[case::buffer_65536(65_536)]
#[case::buffer_65537(65_537)]
#[ignore = "requires the compiled extension; run make test-boundary-parity"]
fn native_boundary_splits_four_byte_scalar_at_large_buffers(#[case] buffer_size: u32) {
    let payload =
        boundary_strategies::patterned_large_witness(buffer_size as usize + 3, "🌱".as_bytes());
    run_boundary_case(&BoundaryCase::new(&payload, buffer_size))
        .unwrap_or_else(|error| panic!("{error}"));
}

#[rstest]
#[case::length_1048572(1_048_572, 4096, b"\xe2\x98\x83")]
#[case::length_1048573(1_048_573, 65_535, b"\xed\xa0\x80")]
#[case::length_1048574(1_048_574, 65_536, b"\xe2\x98")]
#[case::length_1048575(1_048_575, 65_537, b"\xf0\x9f\x8c\xb1")]
#[case::length_1048576(1_048_576, 4096, b"\xf4\x90\x80\x80")]
#[case::length_1048577(1_048_577, 65_535, b"\xe2\x98\x83")]
#[case::length_1048578(1_048_578, 65_536, b"\xed\xa0\x80")]
#[case::length_1048579(1_048_579, 65_537, b"\xe2\x98")]
#[case::length_1048580(1_048_580, 4096, b"\xf0\x9f\x8c\xb1")]
#[ignore = "requires the compiled extension; run make test-boundary-parity"]
fn native_boundary_named_large_length_witnesses(
    #[case] length: usize,
    #[case] buffer_size: u32,
    #[case] tail: &[u8],
) {
    let payload = boundary_strategies::patterned_large_witness(length, tail);
    run_boundary_case(&BoundaryCase::new(&payload, buffer_size))
        .unwrap_or_else(|error| panic!("{error}"));
}

#[test]
#[ignore = "requires the compiled extension; run make test-boundary-parity"]
fn native_boundary_small_properties() {
    for (label, band) in [
        (
            "tiny buffers 1-8",
            boundary_strategies::SmallBufferBand::Tiny,
        ),
        (
            "medium buffers 9-65535",
            boundary_strategies::SmallBufferBand::Medium,
        ),
        (
            "explicit default buffer 65536",
            boundary_strategies::SmallBufferBand::Default,
        ),
        (
            "above-default buffer 65537",
            boundary_strategies::SmallBufferBand::AboveDefault,
        ),
    ] {
        let strategy = (
            boundary_strategies::small_payload_strategy(),
            boundary_strategies::small_buffer_strategy(band),
        );
        run_parity_property(
            strategy,
            &PropertyRun {
                cases: SMALL_CASES_PER_BUFFER_BAND,
                max_shrink_iters: 1024,
                rng_seed: RngSeed::Random,
                persist_failures: true,
                label: label.to_owned(),
            },
        )
        .unwrap_or_else(|error| panic!("{error}"));
    }
}

#[test]
#[ignore = "requires the compiled extension; run make test-boundary-parity"]
fn native_boundary_large_properties() {
    for buffer_size in [4096, 65_535, 65_536, 65_537] {
        let strategy = boundary_strategies::large_payload_strategy()
            .prop_map(move |payload| (payload, buffer_size));
        run_parity_property(
            strategy,
            &PropertyRun {
                cases: LARGE_CASES_PER_BUFFER_SIZE,
                max_shrink_iters: 128,
                rng_seed: RngSeed::Random,
                persist_failures: true,
                label: format!("large payloads with buffer {buffer_size}"),
            },
        )
        .unwrap_or_else(|error| panic!("{error}"));
    }
}

#[test]
#[ignore = "requires the compiled extension; run make test-boundary-parity"]
fn native_boundary_replays_curated_seeds() {
    let seeds = boundary_seeds::read_coverage_seeds().unwrap_or_else(|error| panic!("{error}"));
    assert!(
        !seeds.is_empty(),
        "the curated coverage seed file must not be empty"
    );

    for replay in seeds {
        let strategy = match replay.domain {
            boundary_seeds::SeedDomain::Small => boundary_strategies::small_payload_strategy(),
            boundary_seeds::SeedDomain::Large => boundary_strategies::large_payload_strategy(),
        };
        let first = replay_seed(strategy.clone(), replay.buffer_size, replay.seed)
            .unwrap_or_else(|error| panic!("{}: {error}", replay.label));
        let second = replay_seed(strategy, replay.buffer_size, replay.seed)
            .unwrap_or_else(|error| panic!("{}: {error}", replay.label));
        assert_eq!(
            first, second,
            "{} must replay the same generated case",
            replay.label
        );
    }
}
