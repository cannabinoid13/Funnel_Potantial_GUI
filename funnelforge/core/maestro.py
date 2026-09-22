"""Minimal, fast reader for the Maestro / Desmond ``.cms`` text format.

The format is a nested block language::

    f_m_ct {
      s_m_title
      r_chorus_box_ax
      :::
      "some title"
      85.825271
      m_atom[62870] {
        # First column is atom index #
        i_m_mmod_type
        r_m_x_coord
        :::
        1 26 9.743512 ...
        :::
      }
    }

Only the pieces needed to describe a system geometrically are decoded, and the
row tokeniser stops as soon as the requested columns have been consumed, which
keeps a 50 MB solvated system in the couple-of-seconds range.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')
_ARRAY_START_RE = re.compile(r'^\s*([A-Za-z_]\w*)\[(\d+)\]\s*\{\s*$')
_BLOCK_START_RE = re.compile(r'^\s*([A-Za-z_]\w*)\s*\{\s*$')
_BLOCK_END_RE = re.compile(r'^\s*\}\s*$')
_SEP_RE = re.compile(r'^\s*:::\s*$')

NONE_TOKEN = "<>"


def tokenize(line: str, limit: int | None = None) -> list[str]:
    """Split one Maestro data line into tokens, honouring double quotes.

    ``limit`` stops the scan early (the regex is lazy, so this is a real
    speed-up rather than a slice of a full tokenisation).
    """
    out: list[str] = []
    for m in _TOKEN_RE.finditer(line):
        g = m.group(1)
        out.append(g if g is not None else m.group(2))
        if limit is not None and len(out) >= limit:
            break
    return out


def quote(value: str) -> str:
    """Re-quote a string value the way Maestro does."""
    if value is None:
        return NONE_TOKEN
    s = str(value)
    if s == "":
        return '""'
    if any(c in s for c in ' \t"{}[]#'):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


@dataclass
class ArrayBlock:
    """A ``name[n] { headers ::: rows ::: }`` table, kept as line spans."""

    name: str
    nrows: int
    columns: list[str]
    first_row: int          # index of the first data line in the file buffer
    last_row: int           # index one past the last data line
    start_line: int         # index of the ``name[n] {`` line
    end_line: int           # index of the closing ``}`` line

    def column_index(self, *names: str) -> int | None:
        """1-based token index of the first matching column (0 = atom index)."""
        for n in names:
            if n in self.columns:
                return self.columns.index(n) + 1
        return None


@dataclass
class CtBlock:
    """One ``f_m_ct`` entry: its scalar properties plus the tables it holds."""

    index: int
    start_line: int
    end_line: int
    props: dict[str, str] = field(default_factory=dict)
    arrays: dict[str, ArrayBlock] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.props.get("s_m_title", "") or ""

    @property
    def ct_type(self) -> str:
        return (self.props.get("s_ffio_ct_type", "") or "").strip()

    def prop_float(self, key: str, default: float = 0.0) -> float:
        v = self.props.get(key)
        if v is None or v == NONE_TOKEN:
            return default
        try:
            return float(v)
        except ValueError:
            return default


class MaestroFile:
    """Line-buffer view over a ``.cms`` / ``.mae`` file."""

    def __init__(self, path: str):
        self.path = path
        with open(path, "r", errors="replace") as fh:
            self.lines: list[str] = fh.readlines()
        self.cts: list[CtBlock] = []
        self._scan()

    # -- scanning ---------------------------------------------------------
    def _scan(self) -> None:
        lines = self.lines
        n = len(lines)
        ct_starts = [i for i, ln in enumerate(lines)
                     if ln.startswith("f_m_ct") and "{" in ln]
        for k, start in enumerate(ct_starts):
            end = ct_starts[k + 1] if k + 1 < len(ct_starts) else n
            ct = CtBlock(index=k, start_line=start, end_line=end)
            self._parse_ct(ct)
            self.cts.append(ct)

    def _parse_ct(self, ct: CtBlock) -> None:
        lines = self.lines
        i = ct.start_line + 1
        # --- scalar property table of the ct itself
        headers: list[str] = []
        while i < ct.end_line and not _SEP_RE.match(lines[i]):
            s = lines[i].strip()
            if s and not s.startswith("#"):
                headers.append(s)
            i += 1
        i += 1  # skip ':::'
        values: list[str] = []
        while i < ct.end_line and len(values) < len(headers):
            s = lines[i].strip()
            if s and not s.startswith("#"):
                toks = tokenize(s)
                values.extend(toks)
            i += 1
        ct.props = {h: (v if v != NONE_TOKEN else "")
                    for h, v in zip(headers, values)}

        # --- nested blocks
        while i < ct.end_line:
            ln = lines[i]
            m = _ARRAY_START_RE.match(ln)
            if m:
                name, nrows = m.group(1), int(m.group(2))
                block, i = self._parse_array(name, nrows, i, ct.end_line)
                ct.arrays[name] = block
                continue
            m = _BLOCK_START_RE.match(ln)
            if m:
                # A plain sub-block (e.g. ffio_ff): recurse for its arrays.
                sub_end = self._skip_block(i, ct.end_line)
                j = i + 1
                while j < sub_end:
                    m2 = _ARRAY_START_RE.match(lines[j])
                    if m2:
                        name2, nrows2 = m2.group(1), int(m2.group(2))
                        block, j = self._parse_array(name2, nrows2, j, sub_end)
                        ct.arrays.setdefault(name2, block)
                        continue
                    m3 = _BLOCK_START_RE.match(lines[j])
                    if m3:
                        j = self._skip_block(j, sub_end)
                        continue
                    j += 1
                i = sub_end
                continue
            i += 1

    def _parse_array(self, name: str, nrows: int, start: int, limit: int):
        lines = self.lines
        i = start + 1
        cols: list[str] = []
        while i < limit and not _SEP_RE.match(lines[i]):
            s = lines[i].strip()
            if s and not s.startswith("#"):
                cols.append(s)
            i += 1
        i += 1                      # skip ':::'
        first_row = i
        # Data rows: exactly nrows non-empty lines.
        count = 0
        while i < limit and count < nrows:
            if lines[i].strip():
                count += 1
            i += 1
        last_row = i
        # Trailing ':::' and '}'
        while i < limit and not _BLOCK_END_RE.match(lines[i]):
            i += 1
        end_line = i
        return (ArrayBlock(name=name, nrows=nrows, columns=cols,
                           first_row=first_row, last_row=last_row,
                           start_line=start, end_line=end_line), i + 1)

    def _skip_block(self, start: int, limit: int) -> int:
        """Return the index just past the block that opens on ``start``."""
        depth = 0
        i = start
        while i < limit:
            ln = self.lines[i]
            depth += ln.count("{") - ln.count("}")
            i += 1
            if depth <= 0:
                break
        return i

    # -- row access -------------------------------------------------------
    def rows(self, block: ArrayBlock, limit: int | None = None):
        """Yield tokenised data rows of an array block."""
        lines = self.lines
        for i in range(block.first_row, block.last_row):
            ln = lines[i]
            if not ln.strip():
                continue
            yield tokenize(ln, limit)

    def find_ct(self, ct_type: str) -> CtBlock | None:
        for ct in self.cts:
            if ct.ct_type == ct_type:
                return ct
        return None
