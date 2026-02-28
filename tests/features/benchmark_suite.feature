Feature: Benchmark suite smoke workflow
  Maintainers can generate benchmark run definitions in dry-run mode before
  executing expensive performance jobs.

  Scenario: Generate a benchmark execution plan in smoke dry-run mode
    Given a benchmark output path
    When I generate benchmark plans in smoke dry-run mode
    Then the benchmark plan file exists
    And the benchmark plan indicates a dry run
    And the benchmark plan contains valid scenarios
    And the benchmark plan includes a valid command
    And the plan includes a Python backend scenario
    And the plan records Rust availability

  Scenario: Smoke dry-run plan contains the full scenario matrix
    Given a benchmark output path
    When I generate benchmark plans in smoke dry-run mode
    Then the benchmark plan contains exactly 12 scenarios
    And every scenario name follows the systematic naming convention
    And the scenarios cover all three payload size categories
    And the scenarios cover both pipeline depths
    And the scenarios cover both callback modes
