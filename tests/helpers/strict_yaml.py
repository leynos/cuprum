"""Parse CI documents with a loader that refuses duplicate mapping keys.

PyYAML keeps the last of two identical keys and says nothing. A workflow that
declares ``runs-on`` twice therefore parses into a document that discarded the
first value, so a lane can carry a paid label, a token, or a guard in the
discarded half while every contract reads the half that survived. GitHub's own
parser rejects the file, so the contract and the platform would disagree about
what the repository declares.

Every workflow reader in the test suite parses through :func:`load`, which
refuses the document instead. Scope: CI documents only (workflows and
composite actions); it is not a general-purpose YAML helper.
"""

from __future__ import annotations

import typing as typ

import yaml
from yaml.constructor import ConstructorError

if typ.TYPE_CHECKING:
    from yaml.nodes import MappingNode, Node

#: The tag of a YAML merge key. A merge legitimately supplies keys that the
#: mapping then overrides, so only the mapping's own explicit keys are checked.
_MERGE_TAG = "tag:yaml.org,2002:merge"

#: The context PyYAML's own constructor errors open with.
_CONTEXT = "while constructing a mapping"


class _StrictSafeLoader(yaml.SafeLoader):
    """A ``SafeLoader`` that refuses a mapping declaring one key twice."""

    @typ.override
    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[typ.Hashable, typ.Any]:
        """Refuse a repeated key, then construct the mapping as ``SafeLoader`` does.

        Returns
        -------
        dict[typ.Hashable, typ.Any]
            The constructed mapping.

        Raises
        ------
        ConstructorError
            If the mapping declares the same key twice, marking both places.
        """
        seen: dict[object, Node] = {}
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            key = self.construct_object(key_node, deep=True)
            # `on` and `true` both resolve to True under YAML 1.1, so they
            # collide here exactly as they would in the parsed document.
            if isinstance(key, typ.Hashable) and key in seen:
                problem = f"found duplicate key {key!r}"
                raise ConstructorError(
                    _CONTEXT, seen[key].start_mark, problem, key_node.start_mark
                )
            if isinstance(key, typ.Hashable):
                seen[key] = key_node
        return super().construct_mapping(node, deep=deep)


def load(source: str, name: str) -> object:
    """Parse one CI document, refusing duplicate keys and naming the file.

    Parameters
    ----------
    source : str
        The document's text.
    name : str
        The file name to cite in a failure.

    Returns
    -------
    object
        The parsed document.

    Raises
    ------
    AssertionError
        If the text is not valid YAML or declares a mapping key twice.

    Examples
    --------
    >>> load("jobs: {}", "ci.yml")
    {'jobs': {}}
    """
    try:
        # ruff: ignore[unsafe-yaml-load] - the loader is a SafeLoader subclass.
        return yaml.load(source, Loader=_StrictSafeLoader)
    except yaml.YAMLError as error:
        message = f"{name} is not valid YAML: {error}"
        raise AssertionError(message) from error
