"""Describe the GitHub event a scenario replays through the workflow.

Each event is two things: the name `github.event_name` carries, and the body
`act` writes to the event-path file. The name is not decoration — it selects
which body is built — so both live together in `Event`, and a name the harness
does not implement is refused rather than silently given push semantics.

This is the third seam of the harness. `tests.helpers.act_harness` says what a
scenario *is* (a changed-path set, a staged repository, a run); this module says
what is being replayed *into* it. Splitting them keeps the payload contract
readable on its own, because it is the part that has to match GitHub's.
"""

from __future__ import annotations

import copy
import dataclasses as dc
import enum

from tests.helpers.workflow import mapping

__all__ = ("Event", "EventName", "event_payload")


class EventName(enum.StrEnum):
    """A GitHub event the harness knows how to deliver.

    `event_payload` builds a different body for each, so a name outside this
    set is a harness bug rather than a payload the workflow could parse. The
    member value is the spelling `github.event_name` carries, and a `StrEnum`
    member is a `str`, so either member or value reaches `act`, the payload
    JSON, and the fixtures unchanged.
    """

    PULL_REQUEST = "pull_request"
    PUSH = "push"


@dc.dataclass(frozen=True, slots=True)
class Event:
    """One GitHub event to replay through the workflow.

    Attributes
    ----------
    name : EventName
        Event name, as `github.event_name` would carry it. Only the members of
        `EventName` are deliverable; `event_payload` rejects the rest.
    payload : dict[str, object]
        Webhook payload written to the event-path file. `act` injects
        `github.event_name`, so the payload carries only the body.
    ref : str
        Value for `github.ref`.
    sha : str
        Value for `github.sha`. A pull request is checked out at its head;
        a push is checked out at the commit that was pushed.
    branch : str
        Value for `github.ref_name`, without the `refs/heads/` prefix.
    """

    name: EventName
    payload: dict[str, object]
    ref: str
    sha: str
    branch: str


def event_payload(event: Event, repository: str) -> dict[str, object]:
    """Return the event body `act` should deliver, with its repository filled in.

    Parameters
    ----------
    event : Event
        The event to deliver. Its payload is deep-copied, never mutated.
    repository : str
        ``owner/name`` for the temporary repository the scenario runs in. The
        workflow reads `repository.default_branch` to resolve a push's base.

    Returns
    -------
    dict[str, object]
        The complete webhook payload.

    Raises
    ------
    ValueError
        If `event.name` is not one the harness knows how to deliver. Without
        this, any unrecognized name would silently receive push semantics.
    """
    # `event.name` may be a member or its value, because a `StrEnum` member is
    # a `str`. Converting to the enum is both the membership test and the
    # normalization: one object decides which event is being built, so the
    # branches below cannot disagree about it, and an unknown name is refused
    # here rather than silently given push semantics.
    try:
        name = EventName(event.name)
    except ValueError as exc:
        message = (
            f"unsupported event name {event.name!r}; the harness delivers "
            f"{' and '.join(EventName)}"
        )
        raise ValueError(message) from exc
    payload = copy.deepcopy(event.payload)
    payload["ref"] = event.ref
    match name:
        case EventName.PULL_REQUEST:
            pull_request = mapping(
                payload.get("pull_request"), "event must define pull_request"
            )
            head = mapping(pull_request.get("head"), "pull_request must define head")
            head.update({"sha": event.sha, "ref": event.branch})
        case EventName.PUSH:
            payload["after"] = event.sha
    payload["repository"] = {
        "full_name": repository,
        "default_branch": "main",
        "html_url": f"https://github.com/{repository}",
    }
    payload["sender"] = {"login": "act-harness"}
    return payload
