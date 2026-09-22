"""Span-based editing: change one range of the source and nothing else.

A structured edit in this application never regenerates the file.  It produces
a list of :class:`Edit` objects - each a half-open character range and the
text to put there - which are applied to the original source in one pass.
Everything outside the edited ranges is copied verbatim, so indentation,
comments, numeric spelling, statement order, line endings and any construct
the interface does not understand all survive by construction rather than by
care.

The invariants this module guarantees:

* edits may not overlap; overlapping edits are a programming error and raise,
  because silently choosing a winner would corrupt the file;
* applying an empty edit list returns the source object unchanged;
* :func:`apply` reports, for every edit, where the replacement text ended up,
  so a caller can re-anchor a cursor or a selection afterwards;
* :func:`unchanged_outside` is a machine-checkable statement of "only the
  intended spans moved", and the test suite uses it on every structured edit.
"""

from __future__ import annotations

from dataclasses import dataclass


class OverlappingEdits(ValueError):
    pass


@dataclass(frozen=True)
class Edit:
    """Replace ``src[start:end]`` with ``text``."""

    start: int
    end: int
    text: str
    why: str = ""

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"bad edit range {self.start}..{self.end}")

    @property
    def is_insert(self) -> bool:
        return self.start == self.end

    def delta(self) -> int:
        return len(self.text) - (self.end - self.start)


@dataclass(frozen=True)
class Applied:
    """Where an edit's text ended up in the new document."""

    edit: Edit
    new_start: int
    new_end: int


@dataclass(frozen=True)
class PatchResult:
    text: str
    applied: tuple
    delta: int


def apply(src: str, edits) -> PatchResult:
    """Apply non-overlapping edits to ``src`` in a single pass."""
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    for a, b in zip(ordered, ordered[1:]):
        if b.start < a.end:
            raise OverlappingEdits(
                f"edit {b.start}..{b.end} overlaps {a.start}..{a.end}")
    if ordered and ordered[-1].end > len(src):
        raise ValueError("an edit runs past the end of the source")

    out: list = []
    applied: list = []
    pos = 0
    shift = 0
    for e in ordered:
        out.append(src[pos:e.start])
        new_start = e.start + shift
        out.append(e.text)
        applied.append(Applied(e, new_start, new_start + len(e.text)))
        shift += e.delta()
        pos = e.end
    out.append(src[pos:])
    return PatchResult("".join(out), tuple(applied), shift)


def unchanged_outside(old: str, new: str, edits) -> bool:
    """True when ``new`` differs from ``old`` only inside ``edits``.

    This is the property a structured edit must have.  It is checked by
    rebuilding what the edits alone would produce and comparing; a mismatch
    means something rewrote text it was not asked to touch.
    """
    try:
        return apply(old, edits).text == new
    except (OverlappingEdits, ValueError):
        return False


def replace_node(node, text: str, why: str = "") -> Edit:
    """An edit that swaps one CST node's exact span for new text."""
    return Edit(node.start, node.end, text, why)


def insert_before(node, text: str, why: str = "") -> Edit:
    return Edit(node.start, node.start, text, why)


def insert_after(node, text: str, why: str = "") -> Edit:
    return Edit(node.end, node.end, text, why)


def delete_statement(src: str, node, why: str = "") -> Edit:
    """Remove a statement together with the blank line it leaves behind.

    The span is widened to the start of its own line when only whitespace
    precedes it, and through the following newline, so deleting a statement
    does not leave a stray indent or an empty line where none was intended.
    """
    start = node.start
    line_start = src.rfind("\n", 0, start) + 1
    if src[line_start:start].strip() == "":
        start = line_start
    end = node.end
    nxt = src.find("\n", end)
    if nxt != -1 and src[end:nxt].strip() == "":
        end = nxt + 1
    return Edit(start, end, "", why)


def line_span(src: str, offset: int) -> tuple:
    """The full line containing ``offset``, as a half-open range."""
    start = src.rfind("\n", 0, offset) + 1
    end = src.find("\n", offset)
    return (start, len(src) if end == -1 else end)


def diff_summary(old: str, new: str, context: int = 2) -> str:
    """A unified diff of two revisions, for the confirm-before-save dialog."""
    import difflib
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="current", tofile="proposed", n=context)
    return "".join(lines)
