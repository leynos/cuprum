Feature: Optional Rust stream operations
  Cuprum ships a Rust extension with stream operations that transfer bytes
  between file descriptors or decode output for high-throughput pipelines.

  Scenario: Rust pump stream transfers data between pipes
    Given the Rust pump stream is available
    When I pump a payload through the Rust stream
    Then the output matches the payload

  Scenario: Rust consume stream replaces invalid UTF-8
    Given the Rust consume stream is available
    When I consume a payload with invalid UTF-8
    Then the decoded output matches replacement semantics

  Scenario: Rust pump stream handles large pipe transfers
    Given the Rust pump stream is available
    When I pump a large payload through the Rust stream
    Then the output matches the large payload

  Scenario Outline: Preserve native stream exception categories
    Given the compiled Rust backend is required
    When the <operation> native helper receives <failure>
    Then it raises <exception>

    Examples:
      | operation | failure              | exception  |
      | pump      | a zero buffer size   | ValueError |
      | consume   | a zero buffer size   | ValueError |
      | pump      | a fatal reader error | OSError    |
      | consume   | a fatal reader error | OSError    |
