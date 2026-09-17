MDLINT ?= markdownlint-cli2
# `make fmt` and `make check-fmt` call mdtablefix directly. `--git` selects the
# Markdown files Git tracks and `--include-untracked` adds the untracked files
# Git does not ignore, so a new document is formatted before it is staged.
# Both modes need mdtablefix 0.6.0 or later; CI pins the version at the
# install-mdtablefix step.
MDTABLEFIX ?= mdtablefix
MDTABLEFIX_SELECT = --git --include-untracked
MDTABLEFIX_EXTENSIONS = --md-exts md,markdown,mdx
MDTABLEFIX_RULES = --wrap --renumber --breaks --ellipsis --fences
MARKDOWN_GLOBS = '*.md' '*.markdown' '*.mdx'
NIXIE ?= nixie
YAMLLINT ?= yamllint
ACTIONLINT ?= actionlint
TOOLS = $(MDLINT) $(YAMLLINT) $(ACTIONLINT) uv
VENV_TOOLS = pytest ruff
RUST_DIR ?= rust
CARGO ?= cargo
# Rust 1.85.0 remains Cuprum's published compatibility toolchain. This
# separately pinned nightly is selected only for compatible Linux debug work.
MSRV_TOOLCHAIN ?= 1.85.0
DEV_FAST_TOOLCHAIN ?= nightly-2026-08-23
DEV_FAST_CRANELIFT_COMPONENT ?= rustc-codegen-cranelift
DEV_FAST_CONFIG ?= tools/dev-fast/config.toml
DEV_FAST_MOLD_VERSION_FILE ?= tools/mold/VERSION
DEV_FAST_MOLD_VERSION := $(strip $(shell tr -d '\r\n' < $(DEV_FAST_MOLD_VERSION_FILE)))
DEV_FAST_MOLD_VERSION_PATTERN = $(subst .,\.,$(DEV_FAST_MOLD_VERSION))
DEV_FAST_RUST_CONFIG ?= ../$(DEV_FAST_CONFIG)
DEV_FAST_CARGO_BRIDGE ?= tools/dev-fast/cargo
DEV_FAST_ABSOLUTE_CONFIG := $(abspath $(DEV_FAST_CONFIG))
DEV_FAST_HOST_IS_LINUX := $(if $(filter Linux,$(shell uname -s)),yes)
# `CARGO` remains a caller-injectable single executable. Maturin can select
# that executable but cannot pass Cargo's configuration-file `--config` form.
DEV_FAST_CARGO_COMMAND = RUSTUP_TOOLCHAIN=$(DEV_FAST_TOOLCHAIN) $(CARGO) --config $(DEV_FAST_RUST_CONFIG)
MSRV_CARGO_COMMAND = RUSTUP_TOOLCHAIN=$(MSRV_TOOLCHAIN) $(CARGO)
RUST_DEBUG_CARGO = $(if $(DEV_FAST_HOST_IS_LINUX),$(DEV_FAST_CARGO_COMMAND),$(CARGO))
RUST_DEBUG_PREREQUISITE = $(if $(DEV_FAST_HOST_IS_LINUX),dev-fast-check)
# Parse release spellings without re-parsing caller-provided flags in a shell.
maturin_release_profile = $(strip $(if $(1),$(if $(and $(filter --profile,$(firstword $(1))),$(filter release,$(word 2,$(1)))),yes,$(call maturin_release_profile,$(wordlist 2,$(words $(1)),$(1))))))
MATURIN_DEVELOP_IS_RELEASE := $(strip $(filter --release -r --profile=release,$(MATURIN_DEVELOP_FLAGS)) $(call maturin_release_profile,$(MATURIN_DEVELOP_FLAGS)))
DEVELOP_DEV_FAST_ENABLED := $(if $(DEV_FAST_HOST_IS_LINUX),$(if $(MATURIN_DEVELOP_IS_RELEASE),,yes))
DEVELOP_DEV_FAST_PREREQUISITE = $(if $(DEVELOP_DEV_FAST_ENABLED),dev-fast-check)
DEVELOP_DEV_FAST_ENV = $(if $(DEVELOP_DEV_FAST_ENABLED),RUSTUP_TOOLCHAIN=$(DEV_FAST_TOOLCHAIN) DEV_FAST_CARGO=$(CARGO) DEV_FAST_CONFIG=$(DEV_FAST_ABSOLUTE_CONFIG) CARGO=$(DEV_FAST_CARGO_BRIDGE))
DEV_FAST_CHECK_COMMAND = test "$(DEV_FAST_HOST_IS_LINUX)" = yes || { printf '%s\n' 'dev-fast is supported only on Linux; use the stable backend on this host' >&2; exit 1; }; test -f "$(DEV_FAST_CONFIG)" || { printf 'dev-fast configuration is missing: %s\n' "$(DEV_FAST_CONFIG)" >&2; exit 1; }; command -v mold >/dev/null 2>&1 || { printf 'mold %s is required for Linux dev-fast builds\n' "$(DEV_FAST_MOLD_VERSION)" >&2; exit 1; }; mold --version | grep -q '^mold $(DEV_FAST_MOLD_VERSION_PATTERN)\($$\|[[:space:]]\)' || { printf 'mold %s is required for Linux dev-fast builds\n' "$(DEV_FAST_MOLD_VERSION)" >&2; exit 1; }; rustup component list --installed --toolchain "$(DEV_FAST_TOOLCHAIN)" | grep -q '^$(DEV_FAST_CRANELIFT_COMPONENT)' || { printf 'install %s for %s before using dev-fast\n' "$(DEV_FAST_CRANELIFT_COMPONENT)" "$(DEV_FAST_TOOLCHAIN)" >&2; exit 1; }
DEV_FAST_TEST_RUSTFLAGS = $(TEST_RUSTFLAGS) $(if $(DEV_FAST_HOST_IS_LINUX),-Clink-arg=-fuse-ld=mold)
DEV_FAST_TEST_COMMAND = if $(LOCAL_TOOL_ENV) command -v cargo-nextest >/dev/null 2>&1; then cd $(RUST_DIR) && CARGO_BUILD_JOBS="$(TEST_CARGO_BUILD_JOBS)" RUSTFLAGS="$(DEV_FAST_TEST_RUSTFLAGS)" $(DEV_FAST_CARGO_COMMAND) nextest run $(TEST_FLAGS) $(BUILD_JOBS); else echo "cargo-nextest not found; falling back to cargo test." >&2; cd $(RUST_DIR) && CARGO_BUILD_JOBS="$(TEST_CARGO_BUILD_JOBS)" RUSTFLAGS="$(DEV_FAST_TEST_RUSTFLAGS)" $(DEV_FAST_CARGO_COMMAND) test $(TEST_FLAGS) $(BUILD_JOBS); fi
WHITAKER ?= whitaker
BUILD_JOBS ?=
RUST_FLAGS ?= -D warnings
RUSTDOC_FLAGS ?= -D warnings
CARGO_FLAGS ?= --all-targets --all-features
CLIPPY_FLAGS ?= $(CARGO_FLAGS) -- $(RUST_FLAGS)
DOC_FLAGS ?= --jobs 1
# nextest's test-thread count. One by default so a developer's laptop keeps a
# core for the editor; CI raises it to the vCPU count of the runner the job is
# billed for, and never above it.
TEST_JOBS ?= 1
TEST_FLAGS ?= $(CARGO_FLAGS) --jobs $(TEST_JOBS)
TEST_RUSTFLAGS ?= $(RUST_FLAGS) -C codegen-units=1
WHITAKER_CARGO_FLAGS ?= $(CARGO_FLAGS) --jobs 1
WHITAKER_RUSTFLAGS ?= $(RUST_FLAGS) -C codegen-units=1
# Extra flags for the `maturin develop` invocation in the `develop` target.
# Empty by default: a debug build is what contributors and the extension-tests
# job want. The benchmark ratchet needs an optimized build, and an optimized
# build is the *only* thing it needs differently, so it passes `--release`
# here rather than restating the three-step build sequence inline.
MATURIN_DEVELOP_FLAGS ?=
# Extra flags for the dependency sync performed by the `build` target. The
# benchmark uses this to omit lint-only tooling from its paid runner.
UV_SYNC_FLAGS ?=
# Extra flags for the `uv run` commands in the `develop` target. The benchmark
# uses `--no-sync` after preparing its narrower development environment.
UV_RUN_FLAGS ?=
# The Windows arm of the extension's `cfg` branches. `make lint` only ever sees
# the host's arm, and the Windows wheel build compiles without `-D warnings`,
# so warn-level regressions behind `#[cfg(windows)]` — dead code left by a
# `#[cfg(unix)]` gate, most of all — would reach main unremarked.
WINDOWS_TARGET ?= x86_64-pc-windows-msvc
# PyO3 cannot probe an interpreter for the target platform, so the ABI version
# must be stated. Keep it in step with the `python-version` the Windows job in
# .github/workflows/build-wheels.yml builds against.
WINDOWS_PYTHON_VERSION ?= 3.13
PYTEST_CARGO_BUILD_JOBS ?= 1
PYTEST_RUSTFLAGS ?= -C codegen-units=1
TEST_CARGO_BUILD_JOBS ?= 1
# Keep pytest serial by default: each batch may compile or reuse Rust artefacts,
# and parallel batches contend on the Cargo build cache with little benefit.
PYTEST_WORKERS ?= 0
PYTEST_TARGETS ?= cuprum/unittests/test_*.py \
  tests/test_ci_*.py \
  tests/test_native_sdist.py \
  scripts/tests/test_boundary_*.py \
  tests/behaviour/test_[a-h]*.py \
  tests/behaviour/test_[i-r]*.py \
  tests/behaviour/test_[s-z]*.py
# The modules gated on the compiled extension. Deliberately not the whole
# suite: with the extension installed, test_pipeline.py trips the descriptor
# close race in issue #124 and aborts the interpreter. One definition here,
# consumed by both the extension-tests CI job and the documented local command.
EXTENSION_TEST_TARGETS ?= cuprum/unittests/test_rust_streams.py \
  cuprum/unittests/test_rust_consume_stream.py \
  cuprum/unittests/test_rust_streams_boundary_property.py \
  cuprum/unittests/test_rust_extension.py \
  cuprum/unittests/test_rust_splice.py \
  cuprum/unittests/test_rust_errno.py \
  cuprum/unittests/test_rust_errno_windows.py \
  cuprum/unittests/test_backend.py \
  cuprum/unittests/test_extension_requirement_guard.py \
  tests/behaviour/test_rust_streams_behaviour.py \
  tests/behaviour/test_rust_extension_behaviour.py \
  tests/behaviour/test_stream_backend_pipeline.py
shell_quote = '$(subst ','"'"',$(1))'
UV_ENV = UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools
ifeq ($(OS),Windows_NT)
LOCAL_TOOL_ENV =
else
LOCAL_TOOL_PATH = $(HOME)/.local/bin:$(HOME)/.bun/bin:$(PATH)
LOCAL_TOOL_ENV = PATH="$(LOCAL_TOOL_PATH)"
endif
UV_RUN_ENV = $(LOCAL_TOOL_ENV) $(UV_ENV)
RUFF_ENV = RAYON_NUM_THREADS=1
# Pin Ruff so `make` invokes the same version as the `ruff==` dev dependency
# in pyproject.toml and the RUFF_VERSION env in .github/workflows/ci.yml.
# Bump all three sites together: a version mismatch causes version-skew lint
# failures because rule sets differ between Ruff releases. The pin-parity
# contract test in cuprum/unittests/test_toolchain_pins.py enforces this.
RUFF_VERSION ?= 0.16.4
RUFF = $(RUFF_ENV) $(UV_RUN_ENV) uv tool run --from $(call shell_quote,ruff==$(RUFF_VERSION)) ruff
# Pin ty so `make` and CI invoke the same typechecker release. ty is pre-1.0
# and diagnostics shift between releases, so an unpinned install breaks the
# typecheck gate without any code change. Bump deliberately and fix new
# diagnostics in the same commit. Kept in sync with the `ty==` dev dependency
# in pyproject.toml and the TY_VERSION env in .github/workflows/ci.yml by the
# same contract test as the Ruff pin.
TY_VERSION ?= 0.0.74
TY = $(UV_RUN_ENV) uv tool run --from $(call shell_quote,ty==$(TY_VERSION)) ty
PYTEST = $(UV_RUN_ENV) uv run pytest
# Interrogate the whole Python estate, not only the production package: tests,
# benchmarks, scripts, and the root conftest document their definitions too.
# Per-scope tuning may be added under a [tool.interrogate] section in
# pyproject.toml; none is configured today.
INTERROGATE_TARGETS ?= benchmarks conftest.py cuprum scripts tests
INTERROGATE = $(UV_RUN_ENV) uv run interrogate --fail-under 100 $(INTERROGATE_TARGETS)
PYLINT_PYTHON ?= pypy
PYLINT_TARGETS ?= benchmarks conftest.py cuprum scripts tests
PYLINT_PYPY_SHIM_REF ?= 726d09f968b4d729ee4b29c71fc732e744854f3b
PYLINT_PYPY_SHIM = git+https://github.com/leynos/pylint-pypy-shim.git@$(PYLINT_PYPY_SHIM_REF)
# Pin pylint itself: the shim ref is pinned but pylint is a floating
# dependency of it, so new pylint releases would otherwise change lint
# behaviour without any repository change (same skew class as ruff above).
PYLINT_VERSION ?= 4.0.7
PYLINT_CACHE ?= .cache/pylint
PYLINT_ENV = PYLINTHOME=$(PYLINT_CACHE)
PYLINT = $(PYLINT_ENV) $(UV_RUN_ENV) uv tool run --python $(PYLINT_PYTHON) \
  --from '$(PYLINT_PYPY_SHIM)' --with 'pylint==$(PYLINT_VERSION)' pylint-pypy
TYPOS_CONFIG_BUILDER_VERSION ?= v0.1.2
TYPOS_CONFIG_BUILDER = $(UV_RUN_ENV) uv tool run --python 3.14 --from \
  "git+https://github.com/leynos/typos-config-builder.git@$(TYPOS_CONFIG_BUILDER_VERSION)" \
  typos-config-builder
# Use the controlled v0.3.0 release tag. Keep in step with the
# df12-python-lints dev dependency in pyproject.toml.
DF12_PYTHON_LINTS_REF ?= v0.3.0
DF12_PYTHON_LINTS = git+https://github.com/leynos/df12-python-lints.git@$(DF12_PYTHON_LINTS_REF)
DF12_PYTHON ?= 3.14
DF12_PYLINT_MESSAGES = R9101,C9102,R9103,R9104,C9105,C9106,C9107,R9108,R9109,R9110,R9111,C9112,R9112
DF12_PYLINT = $(PYLINT_ENV) $(UV_RUN_ENV) uv run --isolated \
  --python $(DF12_PYTHON) --with 'pylint==$(PYLINT_VERSION)' \
  --with '$(DF12_PYTHON_LINTS)' pylint \
  --disable=all --load-plugins=df12_python_lints \
  --enable=$(DF12_PYLINT_MESSAGES)
AMBRLEAKS = $(UV_RUN_ENV) uv run --python $(DF12_PYTHON) ambrleaks
# `git ls-files` covers tracked files and nonignored untracked files without
# traversing ignored paths. The shell filter keeps only regular non-symlink
# files, and prefixes a leading dash so the linter cannot parse it as an option.
MDLINT_FILES_FIND = bash -o pipefail -c 'git ls-files -z --cached --others --exclude-standard -- "$$@" | while IFS= read -r -d "" markdown_file; do if [ -f "$$markdown_file" ] && [ ! -L "$$markdown_file" ]; then case "$$markdown_file" in -*) printf "./%s\0" "$$markdown_file" ;; *) printf "%s\0" "$$markdown_file" ;; esac; fi; done' -- $(MARKDOWN_GLOBS)
MDLINT_FIX_COMMAND = unset FORCE_COLOR; $(LOCAL_TOOL_ENV) xargs -0 -r $(MDLINT) --fix < "$$markdown_files"
MDLINT_CHECK_COMMAND = unset FORCE_COLOR; $(LOCAL_TOOL_ENV) xargs -0 -r $(MDLINT) < "$$markdown_files"
.PHONY: help all clean build build-release lint python-lint rust-lint \
        github-actions-lint \
        lint-windows fmt check-fmt \
        markdownlint spelling \
        nixie test test-python test-rust typecheck \
        test-extension test-markdown-format test-dev-fast-contract \
        develop dev-fast-check dev-build dev-test \
        benchmark-micro benchmark-e2e \
        $(TOOLS) $(VENV_TOOLS)
.NOTPARALLEL: lint

.DEFAULT_GOAL := all

all: build check-fmt lint typecheck test

.venv: pyproject.toml
	$(UV_RUN_ENV) uv venv --clear

build: uv .venv ## Build virtual-env and install deps
	$(UV_RUN_ENV) uv sync --group dev $(UV_SYNC_FLAGS)

# Why this exists and why `ensurepip` comes first: see "Building the
# extension for tests" in docs/developers-guide.md.
develop: build $(DEVELOP_DEV_FAST_PREREQUISITE) ## Build the native extension into the dev virtual-env
	$(UV_RUN_ENV) uv run $(UV_RUN_FLAGS) python -m ensurepip --upgrade
	$(DEVELOP_DEV_FAST_ENV) $(UV_RUN_ENV) uv run $(UV_RUN_FLAGS) maturin develop $(MATURIN_DEVELOP_FLAGS) --manifest-path $(RUST_DIR)/cuprum-rust/Cargo.toml

build-release: ## Build artefacts (sdist & wheel)
	python -m build --sdist --wheel

clean: ## Remove build artefacts
	rm -rf build dist *.egg-info \
	  .mypy_cache .pytest_cache .coverage coverage.* \
	  lcov.info htmlcov .venv
	find . -type d -name '__pycache__' -print0 | xargs -0 -r rm -rf
	rm -f .typos-oxendict-base.json .typos-oxendict-base.toml
	cd $(RUST_DIR) && $(CARGO) clean

define ensure_tool
	@$(LOCAL_TOOL_ENV) command -v $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required, but not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

define ensure_tool_venv
	@$(LOCAL_TOOL_ENV) $(UV_ENV) uv run which $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required in the virtualenv, but is not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

define run_markdownlint_files
	@markdown_files="$$(mktemp)" || exit $$?; \
	trap 'rm -f "$$markdown_files"' 0; \
	if ! $(MDLINT_FILES_FIND) > "$$markdown_files"; then \
		exit 1; \
	fi; \
	if [ -s "$$markdown_files" ]; then \
		$(1); \
	fi
endef

ifneq ($(strip $(TOOLS)),)
$(TOOLS): ## Verify required CLI tools
	$(call ensure_tool,$@)
endif


ifneq ($(strip $(VENV_TOOLS)),)
.PHONY: $(VENV_TOOLS)
$(VENV_TOOLS): ## Verify required CLI tools in venv
	$(call ensure_tool_venv,$@)
endif

fmt: ruff ## Format sources
	$(RUFF) format
	$(RUFF) check --select I --fix
	cd $(RUST_DIR) && $(CARGO) fmt --all
	$(LOCAL_TOOL_ENV) $(MDTABLEFIX) --in-place $(MDTABLEFIX_SELECT) $(MDTABLEFIX_EXTENSIONS) $(MDTABLEFIX_RULES)
	$(call run_markdownlint_files,$(MDLINT_FIX_COMMAND))

check-fmt: ruff ## Verify formatting
	$(RUFF) format --check
	cd $(RUST_DIR) && $(CARGO) fmt --all -- --check
	$(LOCAL_TOOL_ENV) $(MDTABLEFIX) --check $(MDTABLEFIX_SELECT) $(MDTABLEFIX_EXTENSIONS) $(MDTABLEFIX_RULES)

test-markdown-format: ## Validate the Markdown formatting Makefile contract
	@PYTHONPATH=scripts $(UV_RUN_ENV) uv run --no-project --python 3.13 \
		--with pytest==9.0.2 --with hypothesis==6.151.9 --with syrupy==6.0.0 \
		python -m pytest scripts/tests/test_markdown_format_makefile.py -c /dev/null \
		--rootdir=. -p no:cacheprovider

lint: python-lint rust-lint github-actions-lint ## Run Python, Rust, and GitHub Actions linters

python-lint: ruff uv ## Run Ruff, interrogate, pylint, df12-python-lints, and ambrleaks
	$(RUFF) check && $(INTERROGATE) && $(PYLINT) $(PYLINT_TARGETS)
	$(DF12_PYLINT) $(PYLINT_TARGETS)
	$(AMBRLEAKS) cuprum/unittests scripts/tests tests

rust-lint: $(RUST_DEBUG_PREREQUISITE) ## Run Rust documentation, Clippy, Whitaker, and spelling checks
	cd $(RUST_DIR) && RUSTDOCFLAGS="$(RUSTDOC_FLAGS)" $(RUST_DEBUG_CARGO) doc --no-deps $(DOC_FLAGS) && $(RUST_DEBUG_CARGO) clippy $(CLIPPY_FLAGS)
	@if ! $(LOCAL_TOOL_ENV) command -v $(WHITAKER) >/dev/null 2>&1; then echo "whitaker is required for linting. Install it before running this target." >&2; exit 1; fi
	cd $(RUST_DIR) && $(LOCAL_TOOL_ENV) RUSTFLAGS="$(WHITAKER_RUSTFLAGS)" $(WHITAKER) --all -- $(WHITAKER_CARGO_FLAGS)
	+$(MAKE) spelling

github-actions-lint: $(YAMLLINT) $(ACTIONLINT) ## Validate GitHub Actions workflows
	$(YAMLLINT) --strict --config-file .yamllint.yml .github/workflows
	$(ACTIONLINT) -config-file .github/actionlint.yaml

lint-windows: ## Lint the Rust extension's Windows cfg branches (cross-target)
	@if ! rustup target list --installed | grep -qx '$(WINDOWS_TARGET)'; then \
	  echo "The $(WINDOWS_TARGET) standard library is required." >&2; \
	  echo "Install it with: rustup target add $(WINDOWS_TARGET)" >&2; \
	  exit 1; \
	fi
	cd $(RUST_DIR) && PYO3_CROSS_PYTHON_VERSION=$(WINDOWS_PYTHON_VERSION) \
	  $(CARGO) clippy --target $(WINDOWS_TARGET) $(CLIPPY_FLAGS)

typecheck: build ## Run typechecking
	$(UV_RUN_ENV) uv sync --group dev
	$(TY) --version
	$(TY) check --python .venv

markdownlint: $(MDLINT) ## Lint Markdown files
	$(call run_markdownlint_files,$(MDLINT_CHECK_COMMAND))
	+$(MAKE) spelling

spelling: ## Enforce en-GB-oxendict spelling in prose and source
	$(TYPOS_CONFIG_BUILDER) gate --repository . --scope all

nixie: ## Validate Mermaid diagrams
	$(call ensure_tool,nixie)
	$(LOCAL_TOOL_ENV) $(NIXIE) --no-sandbox

# Both suites, which is what a contributor wants locally. CI splits them: the
# coverage job is the only place the Rust suite runs, under instrumentation, so
# the interpreter matrix calls `test-python` alone. See "One execution per
# suite" in docs/developers-guide.md.
test: test-python test-rust ## Run the Python and Rust suites

test-python: build uv $(VENV_TOOLS) ## Run the Python suite
	@for pattern in $(foreach target,$(PYTEST_TARGETS),$(call shell_quote,$(target))); do \
	  set -- $$pattern; [ -e "$$1" ] || continue; \
	  CARGO_BUILD_JOBS="$(PYTEST_CARGO_BUILD_JOBS)" RUSTFLAGS="$(PYTEST_RUSTFLAGS)" $(PYTEST) -v -n $(PYTEST_WORKERS) "$$@" || exit $$?; \
	done

test-rust: $(RUST_DEBUG_PREREQUISITE) ## Run the Rust suite
	@if $(LOCAL_TOOL_ENV) command -v cargo-nextest >/dev/null 2>&1; then \
	  cd $(RUST_DIR) && CARGO_BUILD_JOBS="$(TEST_CARGO_BUILD_JOBS)" RUSTFLAGS="$(DEV_FAST_TEST_RUSTFLAGS)" $(RUST_DEBUG_CARGO) nextest run $(TEST_FLAGS) $(BUILD_JOBS); \
	else \
	  echo "cargo-nextest not found; falling back to cargo test." >&2; \
	  cd $(RUST_DIR) && CARGO_BUILD_JOBS="$(TEST_CARGO_BUILD_JOBS)" RUSTFLAGS="$(DEV_FAST_TEST_RUSTFLAGS)" $(RUST_DEBUG_CARGO) test $(TEST_FLAGS) $(BUILD_JOBS); \
	fi

msrv-check: ## Verify every Rust target compiles on the published MSRV
	cd $(RUST_DIR) && $(MSRV_CARGO_COMMAND) check --workspace --all-targets --all-features

dev-fast-check: ## Verify Linux dev-fast prerequisites
	@$(DEV_FAST_CHECK_COMMAND)

dev-build: dev-fast-check ## Build Rust debug targets through the accelerated route
	cd $(RUST_DIR) && $(DEV_FAST_CARGO_COMMAND) build $(CARGO_FLAGS)

dev-test: dev-fast-check ## Test Rust through the accelerated route
	@$(DEV_FAST_TEST_COMMAND)

# Run `make develop` first. Without the extension the guard fails the run with
# a message naming that command, which is the intended diagnostic.
test-extension: build uv $(VENV_TOOLS) ## Run the extension-gated tests, requiring the extension
	CUPRUM_REQUIRE_RUST_EXTENSION=1 $(PYTEST) -v $(EXTENSION_TEST_TARGETS)

test-dev-fast-contract: build uv $(VENV_TOOLS) ## Validate dev-fast routing and adapter contracts
	$(PYTEST) -v cuprum/unittests/test_dev_fast_contract.py cuprum/unittests/test_dev_fast_adapter.py cuprum/unittests/test_dev_fast_prerequisites.py tests/test_dev_fast_action.py

benchmark-micro: build uv ## Run pytest-benchmark microbenchmarks
	mkdir -p dist/benchmarks
	$(UV_RUN_ENV) CUPRUM_RUN_BENCHMARKS=1 uv run pytest -q \
	  benchmarks/test_stream_microbenchmarks.py \
	  --benchmark-json=dist/benchmarks/microbenchmarks.json

benchmark-e2e: build uv ## Run hyperfine end-to-end throughput benchmark
	mkdir -p dist/benchmarks
	$(UV_RUN_ENV) uv run python benchmarks/pipeline_throughput.py \
	  --output dist/benchmarks/pipeline-throughput.json

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-20s %s\n", $$1, $$2}'

# Boundary verifiers use their own pinned binary toolchains; normal gates keep
# rust/rust-toolchain.toml. Kani install is deliberately not source-built here.
PROVER_TOOLS_SOURCE ?= git+https://github.com/leynos/rust-prover-tools@98929b558253659a0a8ae03be7c49dafeef5f673
PROVER_TOOLS = $(UV_RUN_ENV) uv tool run --python 3.14 --from $(PROVER_TOOLS_SOURCE) prover-tools
VERUS_INSTALL_DIR ?= $(HOME)/.local/share/cuprum-verus-0.2026.09.06.8dea4a2
MIRI_TOOLCHAIN = nightly-2026-08-07
KANI_VERSION = 0.67.0
KANI_LIBRARY_PATH = $(HOME)/.kani/kani-$(KANI_VERSION)/toolchain/lib:$(HOME)/.kani/kani-$(KANI_VERSION)/lib

.PHONY: install-verus boundary-verus boundary-kani boundary-miri boundary-test
install-verus: ## Install the checksum-verified prebuilt Verus release
	$(PROVER_TOOLS) verus install --install-dir $(VERUS_INSTALL_DIR)
	$(UV_RUN_ENV) uv run python scripts/install_boundary_z3.py

boundary-verus: ## Verify actual production length/accounting kernels
	$(UV_RUN_ENV) uv run python scripts/render_boundary_proofs.py
	VERUS_Z3_PATH=$(CURDIR)/.cache/boundary-z3/z3 RUSTUP_TOOLCHAIN=1.98.0 $(VERUS_INSTALL_DIR)/verus/verus rust/target/boundary-verification/progress.rs --crate-type=lib

boundary-kani: ## Check bounded native ownership and existing policy proofs
	$(PROVER_TOOLS) kani check-version
	cd $(RUST_DIR) && LD_LIBRARY_PATH="$(KANI_LIBRARY_PATH)" $(CARGO) kani --package cuprum-native-io
	cd $(RUST_DIR) && LD_LIBRARY_PATH="$(KANI_LIBRARY_PATH)" $(CARGO) kani --package cuprum-streams

boundary-miri: ## Interpret isolated native resource and memory paths
	cd $(RUST_DIR) && $(CARGO) +$(MIRI_TOOLCHAIN) miri test --package cuprum-native-io --lib

boundary-test: ## Run isolated native integration and verification-tool contracts
	cd $(RUST_DIR) && $(CARGO) test --package cuprum-native-io --lib
	$(UV_RUN_ENV) uv run pytest scripts/tests/test_boundary_*.py

.PHONY: install-boundary-kani
install-boundary-kani: ## Install checksum-verified Kani binaries without a source build
	$(UV_RUN_ENV) uv run python scripts/install_boundary_kani.py
	PATH="$(CURDIR)/.cache/boundary-kani/bin:$(PATH)" cargo kani setup --use-local-bundle $(CURDIR)/.cache/boundary-kani/kani-$(KANI_VERSION)-x86_64-unknown-linux-gnu.tar.gz

.PHONY: boundary-contract
boundary-contract: ## Confirm actual safe targets reject all forms of unsafe Rust
	$(UV_RUN_ENV) uv run python scripts/check_boundary_contract.py

.PHONY: boundary-faults
boundary-faults: ## Require proof and real-unwind regressions to detect deliberate faults
	$(UV_RUN_ENV) uv run python -m scripts.check_boundary_faults
