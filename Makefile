MDLINT ?= markdownlint-cli2
NIXIE ?= nixie
MDFORMAT_ALL ?= mdformat-all
TOOLS = $(MDFORMAT_ALL) $(MDLINT) uv
VENV_TOOLS = pytest ruff
RUST_DIR ?= rust
CARGO ?= cargo
WHITAKER ?= whitaker
BUILD_JOBS ?= -j1
# Default pytest to a serial run on shared development hosts; set
# PYTEST_WORKERS=N to opt into pytest-xdist when the machine has capacity.
PYTEST_WORKERS ?= 0
RUST_FLAGS ?= -D warnings
RUSTDOC_FLAGS ?= -D warnings
CARGO_FLAGS ?= --all-targets --all-features
CLIPPY_FLAGS ?= $(CARGO_FLAGS) -- $(RUST_FLAGS)
TEST_FLAGS ?= $(CARGO_FLAGS)
TYPOS_VERSION ?= 1.48.0
TYPOS := uv tool run typos@$(TYPOS_VERSION)
UV_ENV = UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools
LOCAL_TOOL_PATH = $(HOME)/.local/bin:$(HOME)/.bun/bin:$(PATH)
LOCAL_TOOL_ENV = PATH="$(LOCAL_TOOL_PATH)"
BUILD_JOBS_VALUE = $(patsubst -j%,%,$(BUILD_JOBS))
CARGO_JOB_ENV = RAYON_NUM_THREADS=$(BUILD_JOBS_VALUE) CARGO_BUILD_JOBS=$(BUILD_JOBS_VALUE)
UV_RUN_ENV = $(LOCAL_TOOL_ENV) $(UV_ENV)
RUFF = $(UV_RUN_ENV) uv run ruff
ifneq ($(strip $(PYTEST_WORKERS)),)
ifneq ($(strip $(PYTEST_WORKERS)),0)
PYTEST_XDIST_FLAGS = -n $(PYTEST_WORKERS)
endif
endif
PYLINT_PYTHON ?= pypy
PYLINT_TARGETS ?= benchmarks conftest.py cuprum tests
PYLINT_PYPY_SHIM_REF ?= 726d09f968b4d729ee4b29c71fc732e744854f3b
PYLINT_PYPY_SHIM = git+https://github.com/leynos/pylint-pypy-shim.git@$(PYLINT_PYPY_SHIM_REF)
# Pin pylint itself: the shim ref is pinned but pylint is a floating
# dependency of it, so new pylint releases would otherwise change lint
# behaviour without any repository change (same skew class as ruff above).
PYLINT_VERSION ?= 4.0.5
PYLINT = $(UV_RUN_ENV) uv tool run --python $(PYLINT_PYTHON) \
  --from '$(PYLINT_PYPY_SHIM)' --with 'pylint==$(PYLINT_VERSION)' pylint-pypy

.PHONY: help all clean build build-release lint fmt check-fmt \
        markdownlint spelling spelling-helper-test nixie test typecheck \
        benchmark-micro benchmark-e2e \
        $(TOOLS) $(VENV_TOOLS)

.DEFAULT_GOAL := all

all: build check-fmt lint typecheck test

.venv: pyproject.toml
	$(UV_RUN_ENV) uv venv --clear

build: uv .venv ## Build virtual-env and install deps
	$(UV_RUN_ENV) uv sync --group dev

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

lint: ruff uv ## Run linters (Ruff, pylint, Clippy, Whitaker)
	$(RUFF) check
	$(UV_RUN_ENV) uv run interrogate --fail-under 100 cuprum
	$(PYLINT) $(PYLINT_TARGETS)
	cd $(RUST_DIR) && RUSTDOCFLAGS="$(RUSTDOC_FLAGS)" $(CARGO_JOB_ENV) $(CARGO) doc --no-deps $(BUILD_JOBS)
	cd $(RUST_DIR) && $(CARGO_JOB_ENV) $(CARGO) clippy $(BUILD_JOBS) $(CLIPPY_FLAGS)
	@if ! $(LOCAL_TOOL_ENV) command -v $(WHITAKER) >/dev/null 2>&1; then \
	  echo "whitaker is required for linting. Install it before running this target." >&2; \
	  exit 1; \
	fi
	cd $(RUST_DIR) && $(CARGO_JOB_ENV) $(LOCAL_TOOL_ENV) RUSTFLAGS="$(RUST_FLAGS)" $(WHITAKER) --all -- $(CARGO_FLAGS) $(BUILD_JOBS)
	+$(MAKE) spelling

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
		--cov=generate_typos_config --cov=typos_rollout \
		--cov=typos_rollout_cache --cov-fail-under=90

nixie: ## Validate Mermaid diagrams
	$(call ensure_tool,nixie)
	$(LOCAL_TOOL_ENV) $(NIXIE) --no-sandbox

test: build uv $(VENV_TOOLS) ## Run tests
	$(UV_RUN_ENV) uv run pytest -v $(PYTEST_XDIST_FLAGS)
	@if $(LOCAL_TOOL_ENV) command -v cargo-nextest >/dev/null 2>&1; then \
	  cd $(RUST_DIR) && RUSTFLAGS="$(RUST_FLAGS)" $(CARGO_JOB_ENV) $(CARGO) nextest run $(TEST_FLAGS) $(BUILD_JOBS); \
	else \
	  echo "cargo-nextest not found; falling back to cargo test." >&2; \
	  cd $(RUST_DIR) && RUSTFLAGS="$(RUST_FLAGS)" $(CARGO_JOB_ENV) $(CARGO) test $(TEST_FLAGS) $(BUILD_JOBS); \
	fi

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
