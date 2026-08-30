Feature: Optional Rust extension availability
  Cuprum ships a pure Python wheel and optional native wheels that include a
  Rust extension. The Rust extension is optional and can be probed without
  breaking pure Python installations.

  Scenario: Rust extension availability is discoverable
    Given the Cuprum Rust availability probe
    When I check whether the Rust extension is available
    Then the probe returns a boolean
    And the probe agrees with the native module when it is installed

  Scenario: Rust availability rejects a non-boolean resolver result
    Given a Rust availability resolver returning a non-boolean value
    When I check the invalid Rust availability result
    Then the probe rejects the result with TypeError
