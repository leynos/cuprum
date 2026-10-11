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


def test_a_missing_heading_is_reported_rather_than_returned_empty() -> None:
    """The failure a caller most needs to see is the heading it named wrongly."""
    with pytest.raises(AssertionError, match="missing subsection heading"):
        extract_markdown_subsection("### Present\n\nBody.\n", heading="Absent")
