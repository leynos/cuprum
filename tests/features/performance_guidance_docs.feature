Feature: Performance guidance in the users' guide
  Readers should be able to decide which stream backend to use from the
  users' guide without cross-referencing implementation details.

  Scenario: Users can find backend-selection guidance in the users' guide
    Given the users' guide performance section
    When I read the backend-selection guidance
    Then it explains when to use auto, python, and rust
    And it tells me to set CUPRUM_STREAM_BACKEND before first backend resolution
    And it explains that current Rust acceleration applies to inter-stage pumping, not stdout or stderr capture
    And it points me to make benchmark-e2e for workload-specific measurement
