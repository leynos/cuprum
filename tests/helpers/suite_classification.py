"""Classify default-suite modules by the process boundary each one requires.

`PYTEST_TARGETS` decides what the default suite collects, but it says nothing
about *why* one batch is separate from another: some boundaries exist because a
module invokes a real compiler or build tool, and others exist only because
splitting was once a way to keep a constrained machine responsive. The second
kind can be reconsidered. The first cannot, and turning the difference into
data rather than a comment is what lets a contract reject an unsafe merge
instead of trusting a reviewer to remember.

Two classes are recorded here.

`COMPILER_INVOKING_MODULES` names modules that, during a test run, cause a real
build process: `cargo build`, `cargo test --doc`, `cargo check`, `clippy`,
`rustc`, `maturin build`, or `uv build`. These contend on one Cargo build lock
and one shared target tree, so they must not be merged into a group with
non-build modules and must never be handed to an xdist worker beside one.

`FRESH_PROCESS_MODULES` is the stricter class: modules that must keep their own
pytest process. It contains every compiler-invoking module - their session
fixtures build artefacts whose purpose is a real build, so a merged session
would reuse another batch's build - plus the modules that read shared Cargo
state through a real process. Everything else in the default suite was audited
and found to be a merge candidate: no module writes `os.environ` outside
`monkeypatch`, none installs a signal handler, one `monkeypatch.chdir` restores
itself, and the only socket binds an ephemeral port.

The values are reason strings, not booleans, because a classification without a
reason is indistinguishable from a guess. A module absent from both mappings is
a merge candidate; a module whose classification is unknown belongs in
`FRESH_PROCESS_MODULES`, since the conservative reading of an unproven boundary
is that it is real.
"""

from __future__ import annotations

import pathlib as pth
import typing as typ

from tests.helpers.ci_documents import require

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "COMPILER_INVOKING_MODULES",
    "FRESH_PROCESS_MODULES",
    "require_classification_is_well_formed",
    "require_modules_exist",
)

#: Modules that run a real compiler or build tool, mapped to what they run and
#: where the spawn site is. A build is the subject of each of these tests, so
#: the tool cannot be stubbed without deleting the contract under test.
COMPILER_INVOKING_MODULES: typ.Final[dict[str, str]] = {
    "cuprum/unittests/test_maturin_build.py": (
        "runs `python -m maturin build --locked` in release mode through "
        "tests/helpers/maturin.py to inspect the built wheel's contents"
    ),
    "cuprum/unittests/test_rust_pump_debug_abort.py": (
        "runs `maturin build --profile debug` so two pipeline scenarios can "
        "run against Rust I/O in a child interpreter"
    ),
    "cuprum/unittests/test_doctest_warning_contract.py": (
        "runs `cargo test --doc` under the pinned dev-fast nightly to prove "
        "the warning route rejects a warning-bearing doctest"
    ),
    "cuprum/unittests/test_cargo_build_warnings_contract.py": (
        "runs `cargo build`, `cargo clippy` and `cargo doc` through "
        "tests/helpers/cargo_warning_fixture.py to observe warning policy"
    ),
    "tests/test_native_sdist.py": (
        "runs `uv build --sdist` and `maturin sdist` to prove the native Rust "
        "workspace survives packaging"
    ),
    "tests/test_py_typed_marker.py": (
        "runs `uv build` and `maturin` to assert the PEP 561 marker inside "
        "real built wheels and source archives"
    ),
    "scripts/tests/test_boundary_workspace.py": (
        "runs `cargo check --frozen` over a materialized boundary workspace to "
        "prove the copied sources actually compile"
    ),
}

#: Modules that must keep their own pytest process, mapped to the reason the
#: boundary is load-bearing.
FRESH_PROCESS_MODULES: typ.Final[dict[str, str]] = {
    **{
        module: f"fresh build required: {reason}"
        for module, reason in COMPILER_INVOKING_MODULES.items()
    },
    "scripts/tests/test_boundary_contract.py": (
        "reads shared Cargo workspace metadata with a real `cargo metadata` "
        "process, so it must not queue behind a merged group's build lock"
    ),
    "scripts/tests/test_boundary_target_discovery.py": (
        "reads shared Cargo workspace metadata with a real `cargo metadata` "
        "process, so it must not queue behind a merged group's build lock"
    ),
    "cuprum/unittests/test_nextest_config_discovery.py": (
        "reads Cargo and nextest configuration through real `cargo nextest "
        "show-config` processes against the shared workspace"
    ),
}

#: The directory classifications are relative to. Kept as a `Path` because the
#: existence check resolves against the repository root, while a reported path
#: is the string a reader can paste into a command.
_ROOT = pth.Path(__file__).resolve().parents[2]


def require_modules_exist(modules: cabc.Iterable[str]) -> None:
    """Require every classified module to exist on disk.

    A classification is a claim about a file. Once the file is renamed or
    deleted the claim is about nothing, and a later contract that consults the
    mapping would silently stop protecting anything. Reporting the missing
    path is more useful than reporting the merge that the stale entry failed
    to prevent.

    Parameters
    ----------
    modules : Iterable of str
        Repository-relative module paths to check.

    Raises
    ------
    AssertionError
        If any path is missing, or if any entry is not a `.py` file.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    for module in modules:
        path = _ROOT / module
        require(
            condition=path.is_file(),
            message=(
                f"{module} is classified as requiring a process boundary, but "
                "no such file exists. Update the classification with the "
                "module's new path, or delete the entry."
            ),
        )


def require_classification_is_well_formed(
    compiler: cabc.Mapping[str, str],
    fresh: cabc.Mapping[str, str],
) -> None:
    """Require the two classifications to be consistent and non-empty.

    The compiler class must be a subset of the fresh-process class: a module
    that runs a build needs its own process *because* it runs a build, and a
    classification that said otherwise would let a contract merge it.

    Parameters
    ----------
    compiler : collections.abc.Mapping of str to str
        The compiler-invoking classification under test.
    fresh : collections.abc.Mapping of str to str
        The fresh-process classification under test.

    Raises
    ------
    AssertionError
        If either mapping is empty, if a reason is blank, or if a compiler
        module is missing from the fresh-process class.
    """  # ruff: ignore[docstring-extraneous-exception] - AssertionError propagates from require()
    require(
        condition=bool(compiler),
        message=(
            "COMPILER_INVOKING_MODULES is empty, so every guard over it would "
            "pass vacuously; an empty classification is the failure"
        ),
    )
    require(
        condition=bool(fresh),
        message=(
            "FRESH_PROCESS_MODULES is empty, so every guard over it would "
            "pass vacuously; an empty classification is the failure"
        ),
    )
    for name, mapping in (
        ("COMPILER_INVOKING_MODULES", compiler),
        ("FRESH_PROCESS_MODULES", fresh),
    ):
        for module, reason in mapping.items():
            require(
                condition=bool(reason.strip()),
                message=f"{name}[{module!r}] records no reason",
            )
    missing = sorted(set(compiler) - set(fresh))
    require(
        condition=not missing,
        message=(
            "a module that invokes a compiler must also require a fresh "
            f"process, but {missing} are only in COMPILER_INVOKING_MODULES"
        ),
    )
