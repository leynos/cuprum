"""Section extraction must read structure, not text that merely looks like it.

``extract_markdown_subsection`` decides where a documentation section ends, and
every documentation contract test in the suite reads a section through it. That
makes its boundary rule load-bearing: whatever it mistakes for a heading
silently shortens the text a contract asserts against. A truncated section does
not report a boundary error — it reports a *missing clause*, pointing the reader
at the prose rather than at the extractor.

The case that exposed this was a fenced Python example whose comment lines begin
at column zero with ``#``. Those are comments; the extractor read them as
headings. These tests pin the distinction.
"""

from __future__ import annotations

import pytest

from tests.helpers import extract_markdown_subsection


def test_a_column_zero_hash_inside_a_fence_does_not_close_the_section() -> None:
    """A comment line is not a heading, however it is indented."""
    document = (
        "### Timeouts\n\n"
        "Before the example.\n\n"
        "```python\n"
        "# A comment at column zero.\n"
        "value = 1\n"
        "# Another one.\n"
        "```\n\n"
        "After the example.\n\n"
        "### Next section\n"
        "Irrelevant.\n"
    )

    section = extract_markdown_subsection(document, heading="Timeouts")

    assert "After the example." in section, (
        "a fenced example's comments must not close the section they sit in, or "
        f"every clause below them goes unchecked; got {section!r}"
    )
    assert "Next section" not in section


def test_tilde_fences_are_tracked_like_backtick_fences() -> None:
    """Both Markdown fence characters delimit code, so both hide comments."""
    document = (
        "### Timeouts\n\n"
        "~~~python\n"
        "# A comment at column zero.\n"
        "~~~\n\n"
        "After the example.\n"
    )

    section = extract_markdown_subsection(document, heading="Timeouts")

    assert "After the example." in section, (
        f"tilde fences delimit code as backticks do; got {section!r}"
    )


def test_the_fenced_example_is_returned_verbatim() -> None:
    """Nothing may be stripped or masked on the way out.

    Widening the boundary is only safe if the body is still the document's own
    text: contracts assert on phrases *inside* these examples, so an extractor
    that blanked fenced lines to find the boundary would break them instead.
    """
    example = "```python\n# A comment at column zero.\nvalue = 1\n```"
    document = f"### Timeouts\n\n{example}\n\nAfter.\n"

    section = extract_markdown_subsection(document, heading="Timeouts")

    assert example in section, (
        f"the fenced example must survive extraction intact; got {section!r}"
    )


def test_a_longer_fence_holds_a_shorter_one_without_closing() -> None:
    """A nested example is content, not the end of the block that holds it.

    A four-backtick fence exists so a document can *show* a three-backtick
    example. Reading the inner run as a closer ends the block one line in, and
    the comment line below it then looks like a heading — which truncates the
    section silently, the exact failure this helper exists to prevent.
    """
    document = (
        "### Timeouts\n\n"
        "````markdown\n"
        "```python\n"
        "# A nested comment at column zero.\n"
        "value = 1\n"
        "```\n"
        "````\n\n"
        "After the nested example.\n"
    )

    section = extract_markdown_subsection(document, heading="Timeouts")

    assert "After the nested example." in section, (
        "a three-backtick run inside a four-backtick block is content, not a "
        f"closer; reading it as one truncates the section; got {section!r}"
    )


def test_an_indented_fence_is_still_a_fence() -> None:
    """A fence nested in a list item delimits code like any other.

    Documentation in this repository indents examples under bullets, and such a
    fence opens on an indented line while its content can still begin at column
    zero. Recognizing only column-zero fences would leave this case silent.
    """
    document = (
        "### Timeouts\n\n"
        "- A bullet with an example:\n\n"
        "  ```python\n"
        "# A column-zero comment inside an indented fence.\n"
        "  ```\n\n"
        "After the example.\n"
    )

    section = extract_markdown_subsection(document, heading="Timeouts")

    assert "After the example." in section, (
        f"an indented fence still delimits code; got {section!r}"
    )


@pytest.mark.parametrize("heading", ["## Higher", "### Peer", "#### Deeper"])
def test_a_real_heading_after_a_fence_closes_the_section(heading: str) -> None:
    """Fence tracking must not swallow headings that really do end a section.

    A section is closed by a peer or higher heading, and a lower-level heading
    is body text. Without this the extraction could stop honouring boundaries at
    all and still satisfy the tests above.
    """
    document = (
        "### Timeouts\n\n"
        "```python\n"
        "# A comment at column zero.\n"
        "```\n\n"
        "After the example.\n\n"
        f"{heading}\n"
        "Later text.\n"
    )

    section = extract_markdown_subsection(document, heading="Timeouts")

    if heading == "#### Deeper":
        assert "Later text." in section, (
            "a deeper heading is body text and must stay inside the section"
        )
    else:
        assert "Later text." not in section, (
            f"{heading!r} is a peer-or-higher heading and must close the section"
        )


def test_an_indented_marker_does_not_swallow_the_next_real_heading() -> None:
    """Four spaces at the top level is indented code, not a fence opener.

    An unclosed indented marker leaves the fence state open, so every later line
    reads as code and the real heading that ends the section is skipped. The
    failure is silent and one-directional: the section grows to include the
    heading and body of the next one, and the contract reading it asserts against
    another section's prose.
    """
    document = (
        "### Timeouts\n\n"
        "Prose before.\n\n"
        "    ```python\n"
        "    indented code, not a fence\n"
        "\n"
        "### Next section\n\n"
        "Later text.\n"
    )

    section = extract_markdown_subsection(document, heading="Timeouts")

    assert "Later text." not in section, (
        "an indented marker is code, not a fence; reading it as one skips the "
        f"real heading and leaks the next section into this one; got {section!r}"
    )


def test_an_indented_fence_inside_a_list_item_is_still_a_fence() -> None:
    """The indentation limit is measured from the item, not from the document.

    A list item's content indent already accounts for its own nesting, so a
    marker four columns in really is a fence there. The example's comment sits at
    column zero, which is what makes the distinction observable: if the fence
    goes unrecognized the comment reads as a heading and closes the section. That
    is also why a document-absolute three-space limit cannot serve — it fixes the
    case above by breaking this one.
    """
    document = (
        "### Timeouts\n\n"
        "1.  A numbered item whose content indent is four.\n\n"
        "    ```python\n"
        "# A column-zero comment inside the item's fence.\n"
        "    ```\n"
        "\n"
        "After the example.\n"
    )

    section = extract_markdown_subsection(document, heading="Timeouts")

    assert "After the example." in section, (
        "a fence nested four columns into a list item is still a fence; "
        f"got {section!r}"
    )


def test_a_missing_heading_is_reported_rather_than_returned_empty() -> None:
    """The failure a caller most needs to see is the heading it named wrongly."""
    with pytest.raises(AssertionError, match="missing subsection heading"):
        extract_markdown_subsection("### Present\n\nBody.\n", heading="Absent")
