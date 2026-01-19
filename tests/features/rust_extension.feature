Feature: Optional Rust extension availability
  Cuprum ships a pure Python wheel and optional native wheels that include a
  Rust extension. The Rust extension is optional and can be probed without
  breaking pure Python installations.

  Scenario: Rust extension availability is discoverable
    Given the Cuprum Rust backend probe
    When I check whether the Rust extension is available
    Then the probe returns a boolean
    And the probe agrees with the native module when it is installed
