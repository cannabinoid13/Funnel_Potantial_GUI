"""Span-preserving reader/writer for the brace syntax of ``.msj`` and ``.cfg``.

Both Multisim MSJ files and Desmond CFG files are ``key = value`` languages
where a value can be a ``{ block }``, an ``[ array ]``, a quoted string or a
bare token, and where a block may also be introduced by a bare stage name::

    simulate {
      title = "NPT, 310 K"
      restraints.new = [ { name = posre_harm  force_constants = 50.0 } ]
    }

Nothing is re-generated from an abstract model: edits are applied as text
replacements on the exact span of the old value, so comments, ordering and
indentation of everything the user did not touch survive untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_KEY_RE = re.compile(r"[A-Za-z_][\w.\-]*")
_WS_COMMENT = re.compile(r"(?:\s|#[^\n]*)*")


@dataclass
class Node:
    key: str
    kind: str                      # 'block' | 'array' | 'scalar'
    key_start: int = -1
    key_end: int = -1
    value_start: int = -1          # first char of the raw value
    value_end: int = -1            # one past the last char
    inner_start: int = -1          # content span for block/array
    inner_end: int = -1
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None

    def child(self, key: str) -> "Node | None":
        for c in self.children:
            if c.key == key:
                return c
        return None

    def children_named(self, key: str) -> list["Node"]:
        return [c for c in self.children if c.key == key]


class BlockText:
    """A parsed view of a brace-syntax document that can be edited in place."""

    def __init__(self, text: str):
        self.text = text
        self.root = Node("", "block", inner_start=0, inner_end=len(text))
        self._parse_into(self.root)

    # ------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------
    def _skip(self, i: int, end: int) -> int:
        m = _WS_COMMENT.match(self.text, i)
        j = m.end() if m else i
        return min(j, end)

    def _parse_into(self, parent: Node) -> None:
        t = self.text
        i, end = parent.inner_start, parent.inner_end
        while True:
            i = self._skip(i, end)
            if i >= end:
                return
            if t[i] in "}]":
                return
            if t[i] == ",":
                i += 1
                continue
            if t[i] == "{":                     # anonymous block in an array
                node = Node("", "block", value_start=i, parent=parent)
                j = self._match(i, end, "{", "}")
                node.inner_start, node.inner_end = i + 1, j - 1
                node.value_end = j
                parent.children.append(node)
                self._parse_into(node)
                i = j
                continue
            m = _KEY_RE.match(t, i)
            if not m:
                # unparsable token: skip to the next whitespace
                j = i
                while j < end and not t[j].isspace():
                    j += 1
                i = j
                continue
            key, ks, ke = m.group(), m.start(), m.end()
            j = self._skip(ke, end)
            if j < end and t[j] == "=":
                j = self._skip(j + 1, end)
                node, i = self._parse_value(key, ks, ke, j, end, parent)
                parent.children.append(node)
                continue
            if j < end and t[j] == "{":
                node = Node(key, "block", key_start=ks, key_end=ke,
                            value_start=j, parent=parent)
                k = self._match(j, end, "{", "}")
                node.inner_start, node.inner_end = j + 1, k - 1
                node.value_end = k
                parent.children.append(node)
                self._parse_into(node)
                i = k
                continue
            # bare token (rare) - treat as a flag
            node = Node(key, "scalar", key_start=ks, key_end=ke,
                        value_start=ks, value_end=ke, parent=parent)
            parent.children.append(node)
            i = ke

    def _parse_value(self, key, ks, ke, j, end, parent):
        t = self.text
        if j < end and t[j] == "{":
            node = Node(key, "block", key_start=ks, key_end=ke,
                        value_start=j, parent=parent)
            k = self._match(j, end, "{", "}")
            node.inner_start, node.inner_end = j + 1, k - 1
            node.value_end = k
            self._parse_into(node)
            return node, k
        if j < end and t[j] == "[":
            node = Node(key, "array", key_start=ks, key_end=ke,
                        value_start=j, parent=parent)
            k = self._match(j, end, "[", "]")
            node.inner_start, node.inner_end = j + 1, k - 1
            node.value_end = k
            self._parse_into(node)
            return node, k
        # scalar: quoted string or bare token(s) up to end of line
        if j < end and t[j] in "\"'":
            q = t[j]
            k = j + 1
            while k < end and t[k] != q:
                k += 2 if t[k] == "\\" else 1
            k = min(k + 1, end)
        else:
            k = j
            while k < end and t[k] not in "\n#}":
                # MSJ/CFG allow adjacent assignments on one physical line.
                # Keep bare values with spaces (e.g. an ASL expression), but
                # stop before the next key rather than swallowing that field.
                if t[k].isspace() and re.match(
                        r'\s+[A-Za-z_][\w.\-]*\s*(?:=|\{)', t[k:end]):
                    break
                k += 1
            while k > j and t[k - 1].isspace():
                k -= 1
        node = Node(key, "scalar", key_start=ks, key_end=ke,
                    value_start=j, value_end=k, parent=parent)
        return node, k

    def _match(self, i: int, end: int, open_ch: str, close_ch: str) -> int:
        """Index one past the matching close bracket for the one at ``i``."""
        t = self.text
        depth = 0
        k = i
        while k < end:
            c = t[k]
            if c in "\"'":
                q = c
                k += 1
                while k < end and t[k] != q:
                    k += 2 if t[k] == "\\" else 1
                k += 1
                continue
            if c == "#":
                while k < end and t[k] != "\n":
                    k += 1
                continue
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return k + 1
            k += 1
        return end

    # ------------------------------------------------------------------
    # access
    # ------------------------------------------------------------------
    def get(self, path: str, root: Node | None = None) -> Node | None:
        """Look up ``a.b[2].c``; also matches literal dotted keys."""
        node = root or self.root
        for part in _split_path(path):
            name, idx = part
            nxt = None
            cands = node.children_named(name)
            if cands:
                nxt = cands[idx if idx is not None else 0] \
                    if (idx is None or idx < len(cands)) else None
            else:
                # allow a literal dotted key such as `checkpt.write_last_step`
                remaining = name
                nxt = node.child(remaining)
            if nxt is None:
                return None
            node = nxt
        return node

    def raw(self, node: Node) -> str:
        if node is None or node.value_start < 0:
            return ""
        return self.text[node.value_start:node.value_end]

    def value(self, path: str, default=None):
        node = self.get(path)
        if node is None:
            return default
        return unquote(self.raw(node))

    def float_value(self, path: str, default=None):
        v = self.value(path)
        if v is None:
            return default
        try:
            return float(str(v).strip())
        except ValueError:
            return default

    # ------------------------------------------------------------------
    # editing
    # ------------------------------------------------------------------
    def replace_spans(self, edits: list[tuple[int, int, str]]) -> None:
        """Apply (start, end, replacement) edits and re-parse."""
        if not edits:
            return
        for start, end, new in sorted(edits, key=lambda e: -e[0]):
            self.text = self.text[:start] + new + self.text[end:]
        self.root = Node("", "block", inner_start=0, inner_end=len(self.text))
        self._parse_into(self.root)

    def set_value(self, path: str, new_text: str, root: Node | None = None) -> bool:
        node = self.get(path, root)
        if node is None:
            return False
        self.replace_spans([(node.value_start, node.value_end, new_text)])
        return True

    def set_node(self, node: Node, new_text: str) -> None:
        self.replace_spans([(node.value_start, node.value_end, new_text)])

    def insert_in_block(self, block: Node, line: str, indent: str = "  ") -> None:
        """Append a ``key = value`` line just before a block's closing brace."""
        pos = block.inner_end
        text = self.text
        # keep the closing brace on its own line
        prefix = "" if text[:pos].endswith("\n") else "\n"
        self.replace_spans([(pos, pos, f"{prefix}{indent}{line}\n")])


def _split_path(path: str):
    out = []
    for part in path.split("."):
        m = re.match(r"^([^\[]+)(?:\[(\d+)\])?$", part)
        if not m:
            out.append((part, None))
            continue
        out.append((m.group(1), int(m.group(2)) if m.group(2) else None))
    return out


def unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def quote_if_needed(s: str) -> str:
    if s == "":
        return '""'
    if re.fullmatch(r"[-+]?[\w./$@*\[\]]+", s):
        return s
    return '"' + s.replace('"', '\\"') + '"'
