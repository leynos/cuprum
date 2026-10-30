# Architectural decision record (ADR) 012: Linux dev-fast routing

## Status

Accepted on 2026-09-17. Cuprum selects the dev-fast Cargo configuration only
for supported Linux debug work through explicit Makefile routes.

## Date

2026-09-17.

## Context and problem statement

Rust compile latency slows the ordinary development loop, but an accelerated
compiler backend and Linux linker cannot become ambient Cargo configuration.
Release packaging, coverage, formal verification, Whitaker, platform-specific
linting, and published minimum-supported Rust version (MSRV) verification need
the repository's stable backend and their existing toolchain contracts. Cargo
also lets `RUSTFLAGS` replace configured flags, so test routing must preserve
mold selection when it supplies warning and code-generation flags.

Maturin accepts a Cargo executable through `CARGO`, but cannot supply Cargo's
configuration-file option. The configuration must therefore be injected without
letting build scripts recursively invoke the adapter or callers substitute a
different configuration.

## Decision

The Makefile pins a separate dev-fast nightly and requires its
`rustc-codegen-cranelift` component. On Linux, standard debug `develop`,
`test-rust`, and Rust documentation and Clippy invocations select the explicit
`tools/dev-fast/config.toml` fragment. `dev-build` and `dev-test` expose the
same route. The fragment stays outside Cargo's auto-discovered configuration.

The fragment applies Cranelift only to the dev profile and supplies the Linux
mold linker flag. Makefile test routes repeat that linker flag whenever they set
`RUSTFLAGS`, because Cargo chooses that environment source instead of merging
the fragment's target flags. `make dev-fast-check` validates the selected
fragment, the version from `tools/mold/VERSION`, and the installed component
before an accelerated route runs.

The routed fragment path is internal state, not caller input. The Makefile
derives it from one constant assigned with GNU Make's `override` directive, so
neither a make variable on the command line nor an environment variable of the
same name can replace it. A plain or `?=` assignment would not hold: an
ordinary environment variable replaces a `?=` definition, and only `override`
outranks a command-line variable. Without it, a caller could route an
accelerated build through a configuration the project never reviewed while the
prerequisite gate still passed. The prerequisite gate checks that fixed
constant. Anything needing a different Cargo configuration is outside this
route and does not use it.

The Linux-only `tools/dev-fast/cargo` adapter receives the real Cargo
executable from Make and derives the fragment from its own location, so the
configuration it passes is a property of the checked-out tree rather than of
its environment. It adds exactly one `--config` option, exports the real
executable as `CARGO` for child build scripts, rejects caller configuration
options, and replaces itself with Cargo to preserve argv and exit status.

The shared CI action downloads only checksum-pinned upstream mold binaries for
supported Linux architectures. It has no source-build fallback. CI installs the
same nightly and component for debug jobs. The stable route remains mandatory
for release, coverage, verification, Whitaker, MSRV checks, and macOS and
Windows. Every workspace member inherits Rust 1.85.0, and `make msrv-check`
compiles every target at that version without the fragment.

## Consequences

Contributors gain faster Linux debug builds after installing the explicit
prerequisites. Direct Cargo use remains unchanged unless the caller selects the
fragment deliberately. Unsupported platforms retain a clear stable route rather
than a partially working linker setup.

The contract tests execute the composite-action shell step with controlled
commands, test both supported architectures and failure boundaries, model
release flag orderings, and exercise adapter argv and environment behaviour.
They complement Make dry-run tests, which prove routing text but not shell
installer behaviour. Pin changes must update the version metadata, checksums,
configuration digest, tests, and this decision if the routing boundary changes.
