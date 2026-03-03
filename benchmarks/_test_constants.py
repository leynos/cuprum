"""Shared test constants for benchmark scenario validation.

This module provides constants used by both the unit test suite
(``cuprum/unittests/test_benchmark_suite.py``) and the behavioural test suite
(``tests/behaviour/test_benchmark_suite_behaviour.py``).
"""

from __future__ import annotations

import re

# Regex for validating benchmark scenario names across test suites.
_SCENARIO_NAME_PATTERN = re.compile(
    r"^(python|rust)-(small|medium|large)-(single|multi)-(nocb|cb)$",
)
