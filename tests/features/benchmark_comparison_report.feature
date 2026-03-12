Feature: Benchmark comparison report
  Maintainers can generate a Python-versus-Rust comparison report from the
  candidate smoke benchmark artefacts and publish the same summary text to the
  GitHub Actions workflow summary.

  Scenario: Generate a benchmark comparison report and markdown summary
    Given paired candidate benchmark artefacts
    When I run the benchmark comparison report CLI
    Then the comparison report command exits successfully
    And the comparison report JSON contains paired backend rows
    And the comparison summary markdown contains a workflow table

  Scenario: Reject malformed benchmark comparison artefacts
    Given malformed candidate benchmark artefacts
    When I run the benchmark comparison report CLI
    Then the comparison report command exits with malformed-input failure
