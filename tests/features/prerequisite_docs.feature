Feature: Build prerequisites and troubleshooting in the users' guide
  Contributors should be able to find build prerequisites and
  troubleshooting guidance in the users' guide without cross-referencing
  implementation details.

  Scenario: Users' guide documents Rust build prerequisites
    Given the users' guide build prerequisites section
    When I read the build prerequisites
    Then it mentions Rust 1.85 or later
    And it mentions cargo and maturin
    And it mentions rustup for installation
    And it explains how to verify the Rust extension

  Scenario: Users' guide includes troubleshooting guidance
    Given the users' guide troubleshooting section
    When I read the troubleshooting guidance
    Then it addresses missing wheels on unsupported platforms
    And it explains forced fallback behaviour via CUPRUM_STREAM_BACKEND
    And it covers benchmark result interpretation
