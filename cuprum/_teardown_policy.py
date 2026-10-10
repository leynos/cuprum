"""The two settings every teardown route needs, carried as one value.

Terminating anything — one child or a whole pipeline — always needs the same
pair: how long to wait before escalating, and whether this run owns the
process group it is signalling. Those travelled as two separate arguments
through every teardown helper, which made each call site repeat the pairing
and pushed several signatures past the project's argument limit. Carrying them
as one value states the pairing once.

Per-stage ownership makes the pair genuinely one setting rather than two that
happen to travel together: a pipeline spawns each stage under the policy from
its own execution context, so ownership is a sequence indexed by stage while
the grace period is shared. ``owns_group`` therefore accepts either one flag
for a single process or one flag per process, and ``for_index`` resolves the
sequence form down to the single-process form the scalar helpers take.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(frozen=True, slots=True)
class _TeardownPolicy:
    """How long a teardown waits, and whose group it is allowed to signal.

    ``owns_group`` is either one flag covering every process, the
    single-command case, or one flag per process in the caller's own order, the
    pipeline case. A short or absent sequence leaves the remaining processes on
    the direct-child route rather than guessing an owner: an ownership
    bookkeeping slip degrades to signalling only the direct child, which is the
    behaviour every run had before ownership existed, instead of signalling a
    group this run never created.
    """

    grace_period: float
    # Keyword-only so a bare ``True``/``False`` can never be mistaken for the
    # grace period at a call site, and so the flag's meaning is stated where it
    # is set.
    owns_group: cabc.Sequence[bool] | bool = dc.field(default=False, kw_only=True)

    def owns_group_for(self, index: int) -> bool:
        """Return the ownership flag recorded for the process at ``index``."""
        if isinstance(self.owns_group, bool):
            return self.owns_group
        return index < len(self.owns_group) and self.owns_group[index]

    def for_index(self, index: int) -> _TeardownPolicy:
        """Return an equivalent policy resolved to the single process at ``index``."""
        return _TeardownPolicy(
            self.grace_period,
            owns_group=self.owns_group_for(index),
        )
