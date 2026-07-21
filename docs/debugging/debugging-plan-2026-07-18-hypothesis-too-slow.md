# Debugging plan: Hypothesis generation exceeds its health-check budget

**Generated**: 2026-07-18
**Issue ID**: local validation follow-up
**Severity**: Medium
**Falsification sub-agent**: Wyvern (investigation fallback; no Alchemist
agent is available)
**Planning agent boundary**: This document was prepared by the planning agent.
Falsification must be executed by the named sub-agent, not by the planning
agent.

## Problem statement

`make test` reported Hypothesis's `too_slow` health check in
`test_single_and_pipeline_tags_agree_on_shared_keys` while generating
`ctx_tags`. The property should execute within Hypothesis's health-check budget
without suppressing that protection or changing its stated behaviour.

## Context summary

_Table 1: Context summary for the slow-generation health-check observation._

| Aspect | Details |
| --- | --- |
| First observed | 2026-07-18 validation run |
| Reproduction evidence | Observed with seed `617015540253848316034710431685553955` |
| Affected components | `test_stage_observation_builder.py`, `_TAGS` strategy |
| Recent changes | Documentation and timeout-branch review follow-up; this test was untouched |

### Error artefacts

```plaintext
FailedHealthCheck: Data generation is extremely slow: Only produced 0 valid
examples in 1.94 seconds. Health check: too_slow.
```

### Investigation outcome

Wyvern replayed the supplied seed five times; every replay passed in
0.18–0.19 seconds. Sampling 100 `_TAGS` examples took 0.098 seconds and 100
fixed-tag observation constructions took 0.001 seconds. Both hypotheses were
falsified, so no correction or health-check suppression is justified.

## Hypotheses

### H1: The `_TAGS` strategy generates values unexpectedly slowly

**Claim**: Although `_TAGS` is
bounded and non-recursive — a finite key set via `st.sampled_from`,
bounded values (`st.text(max_size=5) | st.integers(0, 9)`), and
`max_size=3` — generating it still exhausts Hypothesis's health-check
budget before a valid dictionary is produced.

**Plausibility**: High — the health check identifies `ctx_tags` generation.

**Prediction**: Replaying the seed while sampling `_TAGS` alone will reproduce
the slow generation; if sampling is quick, generation is not the cause.

#### H1 falsification plan

_Table 2: Falsification steps for the `_TAGS` generation hypothesis._

| Step | Action | Expected Negative Result |
| --- | --- | --- |
| 1 | Inspect `_TAGS` and replay the named test seed. | The test completes without a slow-generation health check. |
| 2 | Time representative samples from `_TAGS`. | Sampling is quick, which disproves strategy generation as the cause. |

**Tooling**: display the strategy with a repository-native command (no
`leta workspace add` indexing step required), then replay the focused
property with its recorded seed:

```bash
rg -n -A15 '^_TAGS = ' cuprum/unittests/test_stage_observation_builder.py
uv run pytest \
  cuprum/unittests/test_stage_observation_builder.py::test_single_and_pipeline_tags_agree_on_shared_keys \
  --hypothesis-seed=617015540253848316034710431685553955
```

**Confidence on falsification**: High; the reproduction directly measures the
suspected boundary.

### H2: Observation construction is slow for otherwise valid tags

**Claim**: `_prepare_execution_observation` or pipeline construction consumes
the health-check budget after valid tags are produced.

**Plausibility**: Medium — the property builds both observation paths per
example.

**Prediction**: Supplying a small fixed tag dictionary will still trigger the
health check or have a comparable per-example cost.

#### H2 falsification plan

_Table 3: Falsification steps for the observation-construction hypothesis._

| Step | Action | Expected Negative Result |
| --- | --- | --- |
| 1 | Run an equivalent focused test with fixed small tags. | It runs promptly, disproving construction cost as the primary cause. |

**Tooling**: Focused pytest test or temporary local instrumentation, reverted
after measurement.

**Confidence on falsification**: Medium; it isolates construction from input
generation.

## Recommended execution order

1. **H1** — the error names the generated argument `ctx_tags`, and this
   is the cheapest decisive check.
2. **H2** — investigate construction only if H1 is falsified.

## Termination criteria

- **Root cause identified**: One hypothesis survives while the other is
  falsified, and a narrow correction makes the focused property pass without
  suppressing Hypothesis health checks.
- **Joint cause confirmed**: Both hypotheses remain supported — neither the
  `_TAGS` generation cost (H1) nor the observation-construction cost (H2) is
  falsified, so the slow example arises from their combined budget. Next step:
  attribute the health-check budget across the two phases (time raw `_TAGS`
  sampling and observation construction separately for the same tags) to find
  the dominant contributor, apply the narrow correction to that phase first,
  then re-measure; if neither phase dominates, split the fix across both and
  re-run the focused property until it passes without suppressing health
  checks.
- **Escalation trigger**: Both hypotheses were falsified. Treat the original
  failure as non-reproducible unless it recurs with new evidence.

## Notes for executing agent

Keep the property representative of tag-merge and pipeline precedence
behaviour. Do not add `HealthCheck.too_slow` suppression or lower the property
coverage solely to hide the failure.
