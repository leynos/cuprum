//! Check the compiled Python extension against the independent Python consumer.

use std::{
    cell::RefCell,
    env,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

use proptest::{
    prelude::*,
    test_runner::{Config, FileFailurePersistence, RngSeed, TestCaseError, TestRunner},
};
use rstest::rstest;

#[path = "support/boundary_seeds.rs"]
mod boundary_seeds;
#[path = "support/boundary_strategies.rs"]
mod boundary_strategies;
#[path = "support/bounded_child.rs"]
mod bounded_child;

const PYTHON_ENV: &str = "CUPRUM_BOUNDARY_PYTHON";
const CHILD_TIMEOUT: Duration = Duration::from_secs(30);
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

fn repository_root() -> Result<PathBuf, String> {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .map(Path::to_path_buf)
        .ok_or_else(|| "cuprum-rust manifest has no repository-root ancestor".to_owned())
}

#[expect(
    clippy::disallowed_methods,
    reason = "Make injects the exact project interpreter for this extension boundary test"
)]
fn configured_project_python() -> Option<PathBuf> { env::var_os(PYTHON_ENV).map(PathBuf::from) }

fn run_boundary_case(payload: &[u8], buffer_size: u32) -> Result<(), String> {
    let python = configured_project_python()
        .ok_or_else(|| format!("{PYTHON_ENV} must name the project interpreter"))?;
    if !python.is_absolute() {
        return Err(format!(
            "project interpreter path is not absolute: {}",
            python.display()
        ));
    }
    let helper = repository_root()?.join("tests/helpers/consume_boundary_oracle.py");
    let mut command = Command::new(&python);
    command
        .arg(helper)
        .arg("--buffer-size")
        .arg(buffer_size.to_string());

    let output = bounded_child::run_child(&mut command, payload, CHILD_TIMEOUT)
        .map_err(|failure| format!("boundary child failed: {failure}"))?;
    if let Some(error) = output.stdin_error {
        return Err(format!(
            "boundary child input transfer failed for payload_bytes={} buffer_size={buffer_size}: \
             {error}",
            payload.len()
        ));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        return Err(format!(
            "boundary child exited with {}; payload_bytes={} buffer_size={buffer_size}; \
             stdout={stdout:?}; stderr={stderr:?}",
            output.status,
            payload.len()
        ));
    }
    if !stdout.lines().any(|line| line.starts_with("PASS ")) {
        return Err(format!(
            "boundary child reported no success marker for payload_bytes={} \
             buffer_size={buffer_size}: {stdout:?}",
            payload.len()
        ));
    }

    let reported_python = stdout
        .lines()
        .find_map(|line| line.strip_prefix("PYTHON_EXECUTABLE="))
        .ok_or_else(|| "boundary child omitted its interpreter path".to_owned())?;
    let reported_native = stdout
        .lines()
        .find_map(|line| line.strip_prefix("NATIVE_MODULE="))
        .ok_or_else(|| "boundary child omitted its native module path".to_owned())?;
    let expected_python = python
        .canonicalize()
        .map_err(|error| format!("could not resolve {PYTHON_ENV}: {error}"))?;
    let actual_python = Path::new(reported_python)
        .canonicalize()
        .map_err(|error| format!("could not resolve reported interpreter: {error}"))?;
    if actual_python != expected_python {
        return Err(format!(
            "boundary child used interpreter {}, expected {}",
            actual_python.display(),
            expected_python.display()
        ));
    }
    let native_path = Path::new(reported_native);
    if !native_path.is_absolute() || !native_path.is_file() {
        return Err(format!(
            "boundary child reported an invalid native module path: {}",
            native_path.display()
        ));
    }
    Ok(())
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
        run_boundary_case(payload.as_bytes(), buffer_size).map_err(TestCaseError::fail)
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
        run_boundary_case(generated.as_bytes(), buffer_size).map_err(TestCaseError::fail)
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
    run_boundary_case(payload, buffer_size).unwrap_or_else(|error| panic!("{error}"));
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
    run_boundary_case(&payload, buffer_size).unwrap_or_else(|error| panic!("{error}"));
}

#[rstest]
#[case::buffer_65535(65_535)]
#[case::buffer_65536(65_536)]
#[case::buffer_65537(65_537)]
#[ignore = "requires the compiled extension; run make test-boundary-parity"]
fn native_boundary_splits_four_byte_scalar_at_large_buffers(#[case] buffer_size: u32) {
    let payload =
        boundary_strategies::patterned_large_witness(buffer_size as usize + 3, "🌱".as_bytes());
    run_boundary_case(&payload, buffer_size).unwrap_or_else(|error| panic!("{error}"));
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
    run_boundary_case(&payload, buffer_size).unwrap_or_else(|error| panic!("{error}"));
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
