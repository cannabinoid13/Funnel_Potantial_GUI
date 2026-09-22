"""A small atom-selection language for building the CV groups.

Grammar (case-insensitive)::

    expr    := term (('and' | 'or') term)*
    term    := 'not' term | '(' expr ')' | predicate
    predicate :=
          all | none
        | protein | nucleic | water | ion | hetero | ligand | solute
        | heavy | hydrogen | backbone | sidechain | ca
        | chain   <list>
        | resname <list>
        | resnum  <ranges>          (aliases: res, residue)
        | name    <list>
        | element <list>
        | index   <ranges>          (1-based, aliases: atom, serial)
        | ct      <ranges>
        | within  <radius> of <expr>
        | same residue as <expr>

Selections return 0-based atom indices.
"""

from __future__ import annotations

import re

import numpy as np

from . import elements


class SelectionError(Exception):
    pass


_TOKEN = re.compile(r"\s*(\(|\)|,|[^\s(),]+)")

KEYWORD_MASKS = {
    "all": None,
    "none": None,
    "protein": "protein",
    "nucleic": "nucleic",
    "water": "water",
    "ion": "ion",
    "hetero": "hetero",
    "solute": "solute",
    "heavy": "heavy",
    "hydrogen": "hydrogen",
    "backbone": "backbone",
    "ca": "ca",
}

VALUE_KEYS = {"chain", "resname", "name", "element", "atomname"}
RANGE_KEYS = {"resnum", "res", "residue", "index", "atom", "serial", "ct"}
BINARY = {"and", "or"}


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            break
        out.append(m.group(1))
        i = m.end()
    return out


class _Parser:
    def __init__(self, tokens: list[str], st):
        self.t = tokens
        self.i = 0
        self.st = st
        self.n = st.n_atoms

    def peek(self) -> str | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def next(self) -> str:
        if self.i >= len(self.t):
            raise SelectionError("unexpected end of selection")
        tok = self.t[self.i]
        self.i += 1
        return tok

    def expect(self, word: str) -> None:
        tok = self.next()
        if tok.lower() != word:
            raise SelectionError(f"expected '{word}', found '{tok}'")

    # -- grammar
    def expr(self) -> np.ndarray:
        left = self.term()
        while True:
            tok = self.peek()
            if tok is None or tok.lower() not in BINARY:
                return left
            op = self.next().lower()
            right = self.term()
            left = (left & right) if op == "and" else (left | right)

    def term(self) -> np.ndarray:
        tok = self.peek()
        if tok is None:
            raise SelectionError("empty selection")
        low = tok.lower()
        if low == "not":
            self.next()
            return ~self.term()
        if tok == "(":
            self.next()
            inner = self.expr()
            close = self.next()
            if close != ")":
                raise SelectionError("missing ')'")
            return inner
        return self.predicate()

    def predicate(self) -> np.ndarray:
        st = self.st
        tok = self.next()
        low = tok.lower()
        if low == "all":
            return np.ones(self.n, dtype=bool)
        if low == "none":
            return np.zeros(self.n, dtype=bool)
        if low == "ligand":
            return st.mask("hetero") & ~st.mask("ion")
        if low == "sidechain":
            return st.mask("protein") & ~np.isin(
                st.atomname, list(elements.BACKBONE_NAMES))
        if low in KEYWORD_MASKS:
            return st.mask(KEYWORD_MASKS[low])
        if low == "within":
            radius = float(self.next())
            self.expect("of")
            other = self.term()
            return self._within(radius, other)
        if low == "same":
            self.expect("residue")
            self.expect("as")
            other = self.term()
            return self._same_residue(other)
        if low in VALUE_KEYS:
            values = self._value_list()
            return self._match_values(low, values)
        if low in RANGE_KEYS:
            ranges = self._range_list()
            return self._match_ranges(low, ranges)
        raise SelectionError(f"unknown selection keyword '{tok}'")

    # -- argument readers
    def _value_list(self) -> list[str]:
        out: list[str] = []
        while True:
            tok = self.peek()
            if tok is None or tok == ")" or tok.lower() in BINARY \
                    or tok.lower() in ("not",):
                break
            if tok == ",":
                self.next()
                continue
            if out and tok.lower() in VALUE_KEYS | RANGE_KEYS | \
                    set(KEYWORD_MASKS) | {"within", "same", "ligand",
                                          "sidechain"}:
                break
            out.append(self.next())
        if not out:
            raise SelectionError("a selection keyword needs a value")
        return out

    def _range_list(self) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for tok in self._value_list():
            m = re.fullmatch(r"(-?\d+)\s*-\s*(-?\d+)", tok)
            if m:
                out.append((int(m.group(1)), int(m.group(2))))
                continue
            m = re.fullmatch(r"(-?\d+)", tok)
            if m:
                v = int(m.group(1))
                out.append((v, v))
                continue
            raise SelectionError(f"'{tok}' is not a number or range")
        return out

    # -- matchers
    def _match_values(self, key: str, values: list[str]) -> np.ndarray:
        st = self.st
        vals = [v.strip().upper() for v in values]
        if key == "chain":
            arr = np.char.upper(st.chain)
        elif key == "resname":
            arr = np.char.upper(st.resname)
        elif key in ("name", "atomname"):
            arr = np.char.upper(st.atomname)
        elif key == "element":
            zs = [elements.SYMBOL_TO_Z.get(v) for v in vals]
            zs = [z for z in zs if z is not None]
            if not zs:
                raise SelectionError(f"unknown element(s): {', '.join(vals)}")
            return np.isin(st.anum, zs)
        else:
            raise SelectionError(f"cannot match '{key}'")
        return np.isin(arr, vals)

    def _match_ranges(self, key: str, ranges) -> np.ndarray:
        st = self.st
        if key in ("resnum", "res", "residue"):
            arr = st.resnum
        elif key in ("index", "atom", "serial"):
            arr = np.arange(1, self.n + 1)
        elif key == "ct":
            arr = st.ct_index + 1
        else:
            raise SelectionError(f"cannot match '{key}'")
        out = np.zeros(self.n, dtype=bool)
        for lo, hi in ranges:
            out |= (arr >= lo) & (arr <= hi)
        return out

    def _within(self, radius: float, other: np.ndarray) -> np.ndarray:
        st = self.st
        idx = np.where(other)[0]
        if idx.size == 0:
            return np.zeros(self.n, dtype=bool)
        try:
            from scipy.spatial import cKDTree
            tree = cKDTree(st.xyz[idx])
            hits = tree.query_ball_point(st.xyz, radius) \
                if hasattr(tree, "query_ball_point") else None
            if hits is None:
                hits = tree.query_ball_tree(cKDTree(st.xyz), radius)
                out = np.zeros(self.n, dtype=bool)
                for group in hits:
                    out[group] = True
                return out
            out = np.array([len(h) > 0 for h in hits], dtype=bool)
            return out
        except ImportError:
            out = np.zeros(self.n, dtype=bool)
            for p in st.xyz[idx]:
                out |= np.linalg.norm(st.xyz - p, axis=1) <= radius
            return out

    def _same_residue(self, other: np.ndarray) -> np.ndarray:
        st = self.st
        out = np.zeros(self.n, dtype=bool)
        for i in np.where(other)[0]:
            r = st.residue_of_atom(int(i))
            if r is not None:
                out[r.first:r.last] = True
        return out


_SHORTHAND_SPLIT = re.compile(r"[,;]+")
_RESNUM_RE = re.compile(r"^([A-Za-z]{2,4})?(-?\d+)([A-Za-z]?)$")


def parse_residue_shorthand(text: str, structure) -> list:
    """Read the short forms people actually type for a residue.

    Accepted, in any of the separators ``:``, space or nothing::

        114            Tyr114        A:114        A 114 OH
        114:OH         TYR114:OH     A:114:CZ     114, 197, 442

    Returns one entry per residue found, as
    ``{"label", "indices", "chain", "resnum", "resname", "atom"}`` with
    1-based indices: every heavy atom of the residue unless an atom name was
    given, in which case just those atoms.
    """
    st = structure
    if st is None:
        raise SelectionError("no structure is loaded")
    chains = {c.upper() for c in st.chains() if c}
    out: list = []
    for item in _SHORTHAND_SPLIT.split(text):
        item = item.strip()
        if not item:
            continue
        fields = [f for f in re.split(r"[:\s]+", item) if f]
        chain = resname = atom = None
        resnum = None
        for f in fields:
            m = _RESNUM_RE.match(f)
            if m and resnum is None:
                if m.group(1):
                    resname = m.group(1).upper()
                resnum = int(m.group(2))
                continue
            up = f.upper()
            if chain is None and len(f) <= 2 and up in chains:
                chain = up
                continue
            if atom is None:
                atom = up
                continue
            raise SelectionError(
                f"could not read {item!r}: too many parts. Use forms like "
                "114, Tyr114, A:114 or A:114:OH.")
        if resnum is None:
            raise SelectionError(
                f"{item!r} does not contain a residue number. Use forms like "
                "114, Tyr114, A:114 or A:114:OH.")
        matches = []
        for r in st.residues:
            if r.resnum != resnum:
                continue
            if chain and str(r.chain).upper() != chain:
                continue
            if resname and str(r.resname).strip().upper() != resname:
                continue
            if atom:
                idx = [i + 1 for i in range(r.first, r.last)
                       if str(st.atomname[i]).upper() == atom]
                if not idx:
                    continue
            else:
                idx = [i + 1 for i in range(r.first, r.last)
                       if st.anum[i] > 1]
            if not idx:
                continue
            matches.append((r, idx))
        # A bare number also hits water and ions, which is never what anyone
        # means; keep them only when nothing else matched.
        solvent = {"water", "ion"}
        real = [(r, i) for r, i in matches
                if not any(st.mask(m)[r.first] for m in solvent)]
        chosen = real or matches
        for r, idx in chosen:
            label = f"{r.resname.strip().capitalize()}{r.resnum}"
            if len(chosen) > 1 and str(r.chain):
                label = f"{r.chain}{label}"
            if atom:
                label += f"_{atom}"
            out.append({"label": label, "indices": idx, "chain": r.chain,
                        "resnum": r.resnum, "resname": r.resname.strip(),
                        "atom": atom, "residue": r})
        if not chosen:
            what = f"residue {resnum}"
            if chain:
                what = f"{chain}:{resnum}"
            if resname:
                what = f"{resname}{resnum}"
            if atom:
                what += f" atom {atom}"
            raise SelectionError(f"{what} was not found in this structure.")
    if not out:
        raise SelectionError("nothing to select")
    return out


def looks_like_shorthand(text: str) -> bool:
    """True when the text is a residue shorthand rather than an expression."""
    low = (text or "").lower()
    if not low.strip():
        return False
    words = set(re.findall(r"[a-z_]+", low))
    keywords = (set(KEYWORD_MASKS) | VALUE_KEYS | RANGE_KEYS | BINARY
                | {"not", "within", "of", "same", "residue", "as", "ligand",
                   "sidechain", "all", "none"})
    return not (words & keywords) and bool(re.search(r"\d", low))


def parse_selection(text: str, structure) -> np.ndarray:
    """0-based atom indices matching ``text``."""
    if structure is None:
        raise SelectionError("no structure is loaded")
    if looks_like_shorthand(text):
        # "114", "Tyr114", "A:114:OH" and friends, so a residue number is
        # always a legal thing to type in any selection box
        groups = parse_residue_shorthand(text, structure)
        idx = sorted({i - 1 for g in groups for i in g["indices"]})
        return np.asarray(idx, dtype=np.int64)
    toks = tokenize(text or "")
    if not toks:
        raise SelectionError("empty selection")
    p = _Parser(toks, structure)
    mask = p.expr()
    if p.i < len(p.t):
        raise SelectionError(f"unexpected trailing text: "
                             f"{' '.join(p.t[p.i:])}")
    return np.where(mask)[0]


def describe_indices(structure, idx0) -> str:
    """A short human summary: chains, residues and element composition."""
    st = structure
    idx0 = np.asarray(idx0, dtype=np.int64)
    idx0 = idx0[(idx0 >= 0) & (idx0 < st.n_atoms)]
    if idx0.size == 0:
        return "empty"
    chains = sorted({str(st.chain[i]) or "-" for i in idx0})
    res = st.residue_signature(idx0)
    zs, counts = np.unique(st.anum[idx0], return_counts=True)
    comp = " ".join(f"{elements.symbol(int(z))}{int(c)}"
                    for z, c in zip(zs, counts))
    res_txt = ", ".join(res[:6]) + (" …" if len(res) > 6 else "")
    return (f"chain {'/'.join(chains)} · {res_txt} · {comp}")


def suggest_expression(structure, idx0) -> str:
    """Best-effort selection expression that reproduces a set of atoms."""
    st = structure
    idx0 = np.asarray(idx0, dtype=np.int64)
    if idx0.size == 0:
        return "none"
    chains = sorted({str(st.chain[i]) for i in idx0})
    resnums = sorted({int(st.resnum[i]) for i in idx0})
    names = sorted({str(st.atomname[i]) for i in idx0})
    heavy = bool((st.anum[idx0] > 1).all())
    bits = []
    if len(chains) == 1:
        bits.append(f"chain {chains[0]}")
    if len(resnums) <= 12:
        bits.append("resnum " + ",".join(str(r) for r in resnums))
    if len(names) <= 6:
        bits.append("name " + ",".join(names))
    if heavy:
        bits.append("heavy")
    return " and ".join(bits) if bits else "index " + ",".join(
        str(i + 1) for i in idx0)
