Feature: Benchmark suite smoke workflow
  Maintainers can generate benchmark run definitions in dry-run mode before
  executing expensive performance jobs.

  Scenario: Generate a benchmark execution plan in smoke dry-run mode
    Given a benchmark output path
    When I generate benchmark plans in smoke dry-run mode
    Then the benchmark plan file exists
    And the plan includes a Python backend scenario
    And the plan records Rust availability
