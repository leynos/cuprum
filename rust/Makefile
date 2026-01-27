.PHONY: help all clean test build release lint fmt check-fmt markdownlint nixie

TARGET ?= cuprum-rust

CARGO ?= cargo
BUILD_JOBS ?=
RUST_FLAGS ?= -D warnings
RUSTDOC_FLAGS ?= -D warnings
CARGO_FLAGS ?= --all-targets --all-features
CLIPPY_FLAGS ?= $(CARGO_FLAGS) -- $(RUST_FLAGS)
TEST_FLAGS ?= $(CARGO_FLAGS)
MDLINT ?= markdownlint-cli2
NIXIE ?= nixie

build: ## Build debug artifacts
	$(CARGO) build $(BUILD_JOBS)

release: ## Build release artifacts
	$(CARGO) build $(BUILD_JOBS) --release

all: check-fmt lint test ## Perform a comprehensive check of code

clean: ## Remove build artifacts
	$(CARGO) clean

test: ## Run tests with warnings treated as errors
	@if command -v cargo-nextest >/dev/null 2>&1; then \
		RUSTFLAGS="$(RUST_FLAGS)" $(CARGO) nextest run $(TEST_FLAGS) $(BUILD_JOBS); \
	else \
		echo "cargo-nextest not found; falling back to cargo test." >&2; \
		RUSTFLAGS="$(RUST_FLAGS)" $(CARGO) test $(TEST_FLAGS) $(BUILD_JOBS); \
	fi

lint: ## Run Clippy with warnings denied
	RUSTDOCFLAGS="$(RUSTDOC_FLAGS)" $(CARGO) doc --no-deps
	$(CARGO) clippy $(CLIPPY_FLAGS)
	@if ! command -v whitaker >/dev/null 2>&1; then \
		echo "whitaker is required for linting. Install it before running this target." >&2; \
		exit 1; \
	fi
	whitaker --all -- $(CARGO_FLAGS)

fmt: ## Format Rust and Markdown sources
	$(CARGO) fmt --all
	mdformat-all

check-fmt: ## Verify formatting
	$(CARGO) fmt --all -- --check

markdownlint: ## Run markdownlint from the repository root
	$(MAKE) -C .. markdownlint

nixie: ## Validate Mermaid diagrams from the repository root
	$(MAKE) -C .. nixie
