"""Parse the JSON event stream `act` emits when it runs a job.

`act --json` reports everything a scenario needs — named outputs, step
verdicts, and the step summary — on one JSON object per line of stdout. Two
details of that format are load-bearing and are the reason this is repository
code with its own tests rather than a few lines inline in a test:

- **The stream is cumulative.** A name set more than once appears more than
  once, with the stale intermediate values ahead of the live one, so a reader
  that takes the first match reports an output a later step had already
  replaced.
- **The in-container summary file is truncated.** `$GITHUB_STEP_SUMMARY` is
  consumed once the summary has been uploaded, so the file is not a source; the
  `summary` command's `content` in the stream is.

Nothing here runs a container or touches the network. The parsing is pure
string handling over a recorded stream, which is what lets its unit tests run
in the default suite on every machine; `tests/helpers/act_harness.py` is the
half that needs a runtime. See
`docs/adr-012-actions-runner-integration-harness.md` for the decision.
"""

from __future__ import annotations

import dataclasses as dc
import json
import shlex
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("ActRun", "shell_join")

#: `act` reports an event under this key when the step published a summary.
_SUMMARY_COMMAND = "summary"
#: ...and under this one when it set a step output.
_SET_OUTPUT_COMMAND = "set-output"
#: Per-step verdict key, and the value it carries for a step that passed.
_STEP_RESULT_KEY = "stepResult"
_STEP_SUCCESS = "success"


@dc.dataclass(frozen=True, slots=True)
class ActRun:
    """What one `act` scenario produced.

    Attributes
    ----------
    exit_code : int
        `act`'s own exit status. Zero unless the job failed or `act` could not
        run it, so a failing detector surfaces here as a non-zero code.
    stdout : str
        The JSON event stream, one object per line.
    stderr : str
        `act`'s diagnostic output.
    argv : tuple[str, ...]
        The command that was run, which is what a failure message needs to be
        reproducible.
    """

    exit_code: int
    stdout: str
    stderr: str
    argv: tuple[str, ...]

    @property
    def events(self) -> list[dict[str, object]]:
        """The parsed JSON stream, ignoring lines `act` did not emit.

        Returns
        -------
        list[dict[str, object]]
            One mapping per JSON object on stdout, in emission order.
        """
        parsed: list[dict[str, object]] = []
        for line in self.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:  # pragma: no cover - non-JSON noise
                continue
            if isinstance(event, dict):
                parsed.append(typ.cast("dict[str, object]", event))
        return parsed

    def outputs(self) -> dict[str, str]:
        """Every `set-output` value the job published.

        The stream is cumulative, so a name set more than once appears more
        than once, with the stale intermediate values ahead of the live one.
        The last value wins; taking the first would report an output that a
        later step had already replaced.

        Returns
        -------
        dict[str, str]
            Output name to its final value.
        """
        named: dict[str, str] = {}
        for event in self.events:
            if event.get("command") != _SET_OUTPUT_COMMAND:
                continue
            name = event.get("name")
            argument = event.get("arg")
            if isinstance(name, str) and isinstance(argument, str):
                named[name] = argument
        return named

    def output(self, name: str) -> str | None:
        """One output's final value, or ``None`` if it was never set.

        Returns
        -------
        str | None
            The output's last value, or ``None``.
        """
        return self.outputs().get(name)

    @property
    def step_results(self) -> dict[str, str]:
        """Each step's verdict, keyed by the name the workflow gave it.

        Returns
        -------
        dict[str, str]
            Step name to `success` or `failure`.
        """
        results: dict[str, str] = {}
        for event in self.events:
            step = event.get("step")
            verdict = event.get(_STEP_RESULT_KEY)
            if isinstance(step, str) and isinstance(verdict, str):
                results[step] = verdict
        return results

    @property
    def failed_steps(self) -> list[str]:
        """The names of every step that did not succeed, in order.

        Returns
        -------
        list[str]
            Failed step names.
        """
        return [
            step
            for step, verdict in self.step_results.items()
            if verdict != _STEP_SUCCESS
        ]

    @property
    def summary(self) -> str:
        """The step summary the job wrote, or ``""`` if it wrote none.

        The summary is recovered from the JSON stream rather than from
        `${GITHUB_STEP_SUMMARY}`, which is truncated inside the container once
        the summary has been uploaded.

        Returns
        -------
        str
            Concatenated `summary` command payloads, in emission order.
        """
        return "".join(
            argument
            for event in self.events
            if event.get("command") == _SUMMARY_COMMAND
            and isinstance(argument := event.get("content"), str)
        )

    def failure_context(self) -> str:
        """Render a message that makes a failed scenario reproducible.

        The workflow exits non-zero on the detector-failure path, and that is
        the path's assertion rather than its failure, so a caller that expects
        a non-zero status still has to say what the run did.

        Returns
        -------
        str
            The command, the exit status, the failed steps, and `act`'s
            diagnostics.
        """
        return (
            f"act exited {self.exit_code}; failed steps: {self.failed_steps}\n"
            f"command: {shell_join(self.argv)}\n"
            f"stdout:\n{self.stdout}\n"
            f"stderr:\n{self.stderr}"
        )


def shell_join(argv: cabc.Sequence[str]) -> str:
    """Render a command so it can be pasted into a shell.

    Returns
    -------
    str
        The arguments, quoted as a shell would need them.
    """
    return " ".join(shlex.quote(argument) for argument in argv)
