"""Recognizing the observe-hook dispatcher's code objects under mutation.

mutmut replaces every mutated function with a trampoline. A record of a call,
or of a scheduled task, points at the renamed copy the trampoline forwards to
(``x_<name>__mutmut_orig`` for the unchanged source and
``x_<name>__mutmut_<n>`` for a mutant), never at the public function's
``__code__``. Tests that attribute dispatcher behaviour by code object would
therefore record nothing inside the mutation sandbox, so they ask this module
whether the code belongs to the dispatcher instead of comparing identity.
"""

from __future__ import annotations


def is_dispatcher_code(code: object, name: str) -> bool:
    """Return whether ``code`` is dispatcher function ``name``'s code."""
    code_name = getattr(code, "co_name", "")
    return code_name == name or code_name.startswith(f"x_{name}__mutmut")
