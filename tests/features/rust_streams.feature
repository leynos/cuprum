Feature: Optional Rust stream pump
  Cuprum ships a Rust extension with a pump function that can transfer bytes
  between file descriptors for high-throughput pipelines.

  Scenario: Rust pump stream transfers data between pipes
    Given the Rust pump stream is available
    When I pump a payload through the Rust stream
    Then the output matches the payload
