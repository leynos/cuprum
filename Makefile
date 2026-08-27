MDLINT ?= markdownlint-cli2
NIXIE ?= nixie
MDFORMAT_ALL ?= mdformat-all
TOOLS = $(MDFORMAT_ALL) $(MDLINT) uv
VENV_TOOLS = pytest ruff
RUST_DIR ?= rust
CARGO ?= cargo
WHITAKER ?= whitaker
BUILD_JOBS ?=
RUST_FLAGS ?= -D warnings
RUSTDOC_FLAGS ?= -D warnings
CARGO_FLAGS ?= --all-targets --all-features
CLIPPY_FLAGS ?= $(CARGO_FLAGS) -- $(RUST_FLAGS)
DOC_FLAGS ?= --jobs 1
TEST_FLAGS ?= $(CARGO_FLAGS) --jobs 1
TEST_RUSTFLAGS ?= $(RUST_FLAGS) -C codegen-units=1
WHITAKER_CARGO_FLAGS ?= $(CARGO_FLAGS) --jobs 1
WHITAKER_RUSTFLAGS ?= $(RUST_FLAGS) -C codegen-units=1
# Extra flags for the `maturin develop` invocation in the `develop` target.
# Empty by default: a debug build is what contributors and the extension-tests
# job want. The benchmark ratchet needs an optimized build, and an optimized
# build is the *only* thing it needs differently, so it passes `--release`
# here rather than restating the three-step build sequence inline.
MATURIN_DEVELOP_FLAGS ?=
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
  tests/behaviour/test_[a-h]*.py \
  tests/behaviour/test_[i-r]*.py \
  tests/behaviour/test_[s-z]*.py
# The modules gated on the compiled extension. Deliberately not the whole
# suite: with the extension installed, test_pipeline.py trips the descriptor
# close race in issue #124 and aborts the interpreter. One definition here,
# consumed by both the extension-tests CI job and the documented local command.
EXTENSION_TEST_TARGETS ?= cuprum/unittests/test_rust_streams.py \
  cuprum/unittests/test_rust_streams_boundary_property.py \
  cuprum/unittests/test_rust_extension.py \
  cuprum/unittests/test_rust_splice.py \
  cuprum/unittests/test_rust_errno.py \
  cuprum/unittests/test_backend.py \
  cuprum/unittests/test_extension_requirement_guard.py \
  tests/behaviour/test_rust_streams_behaviour.py \
  tests/behaviour/test_rust_extension_behaviour.py \
  tests/behaviour/test_stream_backend_pipeline.py
shell_quote = '$(subst ','"'"',$(1))'
TYPOS_VERSION ?= 1.48.0
TYPOS := uv tool run typos@$(TYPOS_VERSION)
UV_ENV = UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools
LOCAL_TOOL_PATH = $(HOME)/.local/bin:$(HOME)/.bun/bin:$(PATH)
LOCAL_TOOL_ENV = PATH="$(LOCAL_TOOL_PATH)"
UV_RUN_ENV = $(LOCAL_TOOL_ENV) $(UV_ENV)
RUFF_ENV = RAYON_NUM_THREADS=1
RUFF = $(RUFF_ENV) $(UV_RUN_ENV) uv run ruff
PYTEST = $(UV_RUN_ENV) uv run pytest
PYLINT_PYTHON ?= pypy
PYLINT_TARGETS ?= benchmarks conftest.py cuprum tests
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
DF12_PYTHON_LINTS_REF ?= 755b26f5792f71b37f3a9e656aef714ed98b2c3b
DF12_PYTHON_LINTS = git+https://github.com/leynos/df12-python-lints.git@$(DF12_PYTHON_LINTS_REF)
DF12_PYTHON ?= 3.14
DF12_PYLINT_MESSAGES = R9101,C9102,R9103,R9104,C9105,C9106,C9107,R9108,R9109,R9110,R9111,C9112
DF12_PYLINT = $(PYLINT_ENV) $(UV_RUN_ENV) uv run --isolated \
  --python $(DF12_PYTHON) --with 'pylint==$(PYLINT_VERSION)' \
  --with '$(DF12_PYTHON_LINTS)' pylint \
  --disable=all --load-plugins=df12_python_lints \
  --enable=$(DF12_PYLINT_MESSAGES)
AMBRLEAKS = $(UV_RUN_ENV) uv run --python $(DF12_PYTHON) ambrleaks
SKYLOS_VERSION = 4.33.2
# Skylos parses source using its own Python AST, so Python 3.14 prevents
# phantom dead-code findings from syntax older tool runtimes cannot parse.
SKYLOS_CLI = $(UV_RUN_ENV) uv tool run --python 3.14 --from 'skylos==$(SKYLOS_VERSION)' skylos
SKYLOS = $(SKYLOS_CLI) --config-file pyproject.toml
SKYLOS_PRODUCTION_TARGETS ?= cuprum
SKYLOS_EXCLUDE_FOLDERS ?= cuprum/unittests
SKYLOS_WHITELIST_LOCK ?= .skylos-whitelist.lock

.PHONY: help all clean build build-release lint python-lint rust-lint \
        lint-windows fmt check-fmt \
        markdownlint spelling spelling-helper-test nixie test typecheck \
        test-extension develop makeutil skylos-allow \
        benchmark-micro benchmark-e2e \
        $(TOOLS) $(VENV_TOOLS)
.NOTPARALLEL: lint

.DEFAULT_GOAL := all

all: build check-fmt lint typecheck test

.venv: pyproject.toml
	$(UV_RUN_ENV) uv venv --clear

build: uv .venv ## Build virtual-env and install deps
	$(UV_RUN_ENV) uv sync --group dev

# Why this exists and why `ensurepip` comes first: see "Building the
# extension for tests" in docs/developers-guide.md.
develop: build ## Build the native extension into the dev virtual-env
	$(UV_RUN_ENV) uv run python -m ensurepip --upgrade
	$(UV_RUN_ENV) uv run maturin develop $(MATURIN_DEVELOP_FLAGS) --manifest-path $(RUST_DIR)/cuprum-rust/Cargo.toml

build-release: ## Build artefacts (sdist & wheel)
	python -m build --sdist --wheel

clean: ## Remove build artifacts
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

ifneq ($(strip $(TOOLS)),)
$(TOOLS): ## Verify required CLI tools
	$(call ensure_tool,$@)
endif


ifneq ($(strip $(VENV_TOOLS)),)
.PHONY: $(VENV_TOOLS)
$(VENV_TOOLS): ## Verify required CLI tools in venv
	$(call ensure_tool_venv,$@)
endif

fmt: ruff $(MDFORMAT_ALL) ## Format sources
	$(RUFF) format
	$(RUFF) check --select I --fix
	cd $(RUST_DIR) && $(CARGO) fmt --all
	$(LOCAL_TOOL_ENV) $(MDFORMAT_ALL)

check-fmt: ruff ## Verify formatting
	$(RUFF) format --check
	cd $(RUST_DIR) && $(CARGO) fmt --all -- --check
	# mdformat-all doesn't currently do checking

lint: python-lint rust-lint ## Run Python and Rust linters

python-lint: ruff uv ## Run Ruff, interrogate, pylint, df12-python-lints, and ambrleaks
	$(RUFF) check && $(UV_RUN_ENV) uv run interrogate --fail-under 100 cuprum && $(PYLINT) $(PYLINT_TARGETS)
	$(DF12_PYLINT) $(PYLINT_TARGETS)
	$(AMBRLEAKS) cuprum/unittests tests
	$(SKYLOS) $(SKYLOS_PRODUCTION_TARGETS) --exclude $(SKYLOS_EXCLUDE_FOLDERS) --category dead_code --gate --format concise --no-upload --no-provenance --no-grep-verify

rust-lint: ## Run Rust documentation, Clippy, Whitaker, and spelling checks
	cd $(RUST_DIR) && RUSTDOCFLAGS="$(RUSTDOC_FLAGS)" $(CARGO) doc --no-deps $(DOC_FLAGS) && $(CARGO) clippy $(CLIPPY_FLAGS)
	@if ! $(LOCAL_TOOL_ENV) command -v $(WHITAKER) >/dev/null 2>&1; then echo "whitaker is required for linting. Install it before running this target." >&2; exit 1; fi
	cd $(RUST_DIR) && $(LOCAL_TOOL_ENV) RUSTFLAGS="$(WHITAKER_RUSTFLAGS)" $(WHITAKER) --all -- $(WHITAKER_CARGO_FLAGS)
	+$(MAKE) spelling

skylos-allow: export SKYLOS_SYMBOL = $(value SYMBOL)
skylos-allow: export SKYLOS_REASON = $(value REASON)
skylos-allow: ## Document one named Skylos exception, not an entry point
	@case "$${SKYLOS_SYMBOL}" in *[![:space:]]*) ;; *) printf "Error: SYMBOL is required for a named whitelist exception\\n" >&2; exit 2;; esac
	@case "$${SKYLOS_REASON}" in *[![:space:]]*) ;; *) printf "Error: REASON is required for a named whitelist exception\\n" >&2; exit 2;; esac
	flock "$(SKYLOS_WHITELIST_LOCK)" env $(SKYLOS_CLI) whitelist "$${SKYLOS_SYMBOL}" --reason "$${SKYLOS_REASON}"

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
	$(UV_RUN_ENV) uv run ty --version
	$(UV_RUN_ENV) uv run ty check

markdownlint: $(MDLINT) ## Lint Markdown files
	$(LOCAL_TOOL_ENV) $(MDLINT) '**/*.md'
	+$(MAKE) spelling

spelling: spelling-helper-test ## Enforce en-GB-oxendict spelling in Markdown prose
	@$(UV_RUN_ENV) uv run scripts/generate_typos_config.py
	@git ls-files -z '*.md' | \
		xargs -0 -r $(TYPOS) --config typos.toml --force-exclude

spelling-helper-test: build ## Validate the shared spelling-policy integration
	@PYTHONPATH=scripts $(UV_RUN_ENV) uv run --python 3.13 \
		--with pytest-cov==7.0.0 \
		python -m pytest scripts/tests/test_typos_rollout.py \
		scripts/tests/test_typos_rollout_properties.py \
		scripts/tests/test_typos_rollout_refresh.py \
		--cov=generate_typos_config --cov=typos_rollout \
		--cov=typos_rollout_cache --cov=typos_rollout_dictionary \
		--cov=typos_rollout_refresh --cov-fail-under=90

nixie: ## Validate Mermaid diagrams
	$(call ensure_tool,nixie)
	$(LOCAL_TOOL_ENV) $(NIXIE) --no-sandbox

makeutil: ## Verify the Makefile parser used by contract tests
	$(call ensure_tool,$@)

test: build uv $(VENV_TOOLS) makeutil ## Run tests
	@for pattern in $(foreach target,$(PYTEST_TARGETS),$(call shell_quote,$(target))); do \
	  set -- $$pattern; [ -e "$$1" ] || continue; \
	  CARGO_BUILD_JOBS="$(PYTEST_CARGO_BUILD_JOBS)" RUSTFLAGS="$(PYTEST_RUSTFLAGS)" $(PYTEST) -v -n $(PYTEST_WORKERS) "$$@" || exit $$?; \
	done
	@if $(LOCAL_TOOL_ENV) command -v cargo-nextest >/dev/null 2>&1; then \
	  cd $(RUST_DIR) && CARGO_BUILD_JOBS="$(TEST_CARGO_BUILD_JOBS)" RUSTFLAGS="$(TEST_RUSTFLAGS)" $(CARGO) nextest run $(TEST_FLAGS) $(BUILD_JOBS); \
	else \
	  echo "cargo-nextest not found; falling back to cargo test." >&2; \
	  cd $(RUST_DIR) && CARGO_BUILD_JOBS="$(TEST_CARGO_BUILD_JOBS)" RUSTFLAGS="$(TEST_RUSTFLAGS)" $(CARGO) test $(TEST_FLAGS) $(BUILD_JOBS); \
	fi

# Run `make develop` first. Without the extension the guard fails the run with
# a message naming that command, which is the intended diagnostic.
test-extension: build uv $(VENV_TOOLS) ## Run the extension-gated tests, requiring the extension
	CUPRUM_REQUIRE_RUST_EXTENSION=1 $(PYTEST) -v $(EXTENSION_TEST_TARGETS)

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
