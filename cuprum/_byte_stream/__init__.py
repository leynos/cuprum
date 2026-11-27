"""Internal implementation of owned live byte streams.

The public vocabulary lives in :mod:`cuprum.byte_streams` and
:mod:`cuprum._process_handle_errors`. This package owns the mechanisms behind
it:

- :mod:`cuprum._byte_stream.byte_queue` — the lossless, byte-bounded queue and
  its terminal state;
- :mod:`cuprum._byte_stream.reader` — the async iterator and closure surface a
  caller uses;
- :mod:`cuprum._byte_stream.wiring` — the per-stream taps the stream spawn
  attaches;
- :mod:`cuprum._byte_stream.owned_start` — ``SafeCmd.start_bytes()``;
- :mod:`cuprum._byte_stream.handle` — ``RunningBytesCommand``;
- :mod:`cuprum._byte_stream.settlement` — bytes result assembly, the settlement
  release point, late-failure reconciliation, and transport release.

The package is split this way so no module reaches the repository's 400-line
ceiling and so each question has one owner: the queue knows capacity and
terminal state, the reader knows iteration and errors, and the handle knows
ownership and settlement.
"""
