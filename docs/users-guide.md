# cuprum Users' Guide

## Program catalogue

Cuprum exposes a typed `Program` NewType to identify executables. The library
ships with a curated catalogue (`DEFAULT_CATALOGUE`) that defines an allowlist
of programs and project metadata. Requests for unknown executables raise
`UnknownProgramError` so accidental shell access is blocked by default.

- Import curated programs from `cuprum.catalogue` (for example `ECHO`, `LS`).
- Each project in the catalogue includes `noise_rules` (output lines that
  downstream loggers can safely drop) and `documentation_locations` (links for
  operators and reviewers).

```python
from cuprum.catalogue import DEFAULT_CATALOGUE, ECHO

entry = DEFAULT_CATALOGUE.lookup(ECHO)
print(entry.project.noise_rules)
```

### Adding project-specific programs

Use `ProjectSettings` and `ProgramCatalogue` to extend or replace the default
catalogue:

```python
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.program import Program

project = ProjectSettings(
    name="data-pipeline",
    programs=(Program("python"),),
    documentation_locations=("docs/pipelines.md",),
    noise_rules=(r"^progress:",),
)
catalogue = ProgramCatalogue(projects=(project,))
```

Downstream services can fetch metadata via `catalogue.visible_settings()` to
propagate noise filters and documentation links alongside the allowlist.
