"""Semantic analysis of a Desmond M-expression: symbols, types, data flow.

Everything here is derived from the *shape of the data flow*, never from what
a variable is called.  A file whose groups are named ``grpA``/``grpB`` and
whose bias is called ``total`` is analysed exactly as well as one using the
conventional ``lig``/``site``/``core``/``v_total``.

What this module answers
------------------------
* which names exist, in which scope, and where each is defined and used;
* what category and dimensionality each value has, as far as the language's
  own signature table allows it to be known;
* which definitions transitively reach the potential, and which only reach a
  side-effecting call such as ``print``;
* every ``meta()`` call at any nesting depth, its accumulator, its collective
  variables, and whether it is a bias, a zero-height probe used to read the
  accumulated bias back, or something this analysis cannot classify;
* whether the well-tempered relation ``h(t) = h0 exp(-V(s,t)/kDT)`` actually
  holds in the data flow, however many intermediate assignments it is spread
  across;
* which parts of the file are opaque, and whether any of them reaches the
  potential - because that is the condition under which a structured export
  would silently drop physics and must therefore be refused.

The analysis never rewrites anything.  It produces a view, addressed by the
same character offsets the CST uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import cst
from .cst import Node, ParseResult, Problem
from . import registry as reg

# -- value categories -------------------------------------------------------
SELECTION = "selection"
NUMBER = "number"
STRING = "string"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Type:
    """A value's category and, where knowable, its array length.

    Desmond has no scalar/vector distinction: every value is an array of
    germs, a scalar simply has length one.  ``dim`` is that length, and
    ``None`` means the analysis could not determine it - which is reported as
    "unknown", never as an error.
    """

    cat: str = UNKNOWN
    dim: int | None = None
    note: str = ""

    def __str__(self) -> str:
        if self.cat == SELECTION:
            return f"selection[{self.dim if self.dim is not None else '?'}]"
        if self.cat == STRING:
            return "string"
        if self.cat == NUMBER:
            if self.dim == 1:
                return "scalar"
            if self.dim == 3:
                return "vector"
            return f"array[{self.dim if self.dim is not None else '?'}]"
        return "unknown"


T_UNKNOWN = Type()
T_SCALAR = Type(NUMBER, 1)
T_VECTOR = Type(NUMBER, 3)
T_STRING = Type(STRING, 1)


@dataclass
class Symbol:
    """One bound name."""

    name: str
    bind: Node                      # the BIND node
    value: Node                     # its right-hand side
    scope: str                      # 'program' or a block/series scope id
    type: Type = T_UNKNOWN
    deps: set = field(default_factory=set)      # names it reads
    uses: list = field(default_factory=list)    # NAME nodes that read it
    reaches_energy: bool = False
    reaches_side_effect: bool = False
    roles: set = field(default_factory=set)

    @property
    def line(self) -> int:
        return self._line

    _line: int = 0
    _col: int = 0


@dataclass
class Declaration:
    """A ``declare_meta`` or ``declare_output`` header form."""

    node: Node
    which: str                      # 'declare_meta' | 'declare_output'
    terms: dict = field(default_factory=dict)   # key -> Node
    index: int = 0                  # position among declare_meta blocks

    def literal(self, key: str):
        n = self.terms.get(key)
        if n is None:
            return None
        return _literal_number(n)

    @property
    def dimension(self) -> int | None:
        v = self.literal("dimension")
        return int(v) if v is not None else None

    @property
    def output_name(self) -> str:
        n = self.terms.get("name")
        if n is not None and n.kind == cst.STR:
            return n.text.strip('"')
        return ""


#: how a meta() call participates in the potential
BIAS = "bias"                 # its value reaches the final energy
PROBE = "probe"               # zero hills: reads the accumulated bias back
INTERMEDIATE = "intermediate"  # feeds another bias but not the energy directly
DIAGNOSTIC = "diagnostic"     # only ever printed
UNDETERMINED = "undetermined"


@dataclass
class WellTempered:
    """Evidence that a hill height follows the well-tempered relation.

    ``ktemp`` and ``h0`` are recovered from the same expression whether or not
    the relation holds, so they mean nothing unless ``confirmed`` is true -
    read them through :meth:`parameters`, which says so, rather than off the
    fields.
    """

    confirmed: bool
    probe: "MetaCall | None"
    exp_node: Node | None
    negation: bool
    ktemp: float | None
    h0: float | None
    evidence: str = ""

    def parameters(self) -> tuple:
        """(kDT, h0) when the relation is proved, (None, None) otherwise."""
        if not self.confirmed:
            return (None, None)
        return (self.ktemp, self.h0)


@dataclass
class MetaCall:
    """One ``meta(...)`` call, wherever it appears in the tree."""

    node: Node
    index: int | None
    index_node: Node | None
    hills: Node | None
    cvs: Node | None
    n_hills: int | None
    n_cvs: int | None
    zero_height: bool
    owner: str = ""                 # the name it is bound to, if any
    role: str = UNDETERMINED
    declared: Declaration | None = None
    wt: WellTempered | None = None
    depth: int = 0


@dataclass
class Contribution:
    """One additive term of the final energy, as written."""

    node: Node
    label: str
    symbol: Symbol | None
    opaque: bool = False
    kinds: set = field(default_factory=set)     # 'meta', 'wall', 'unknown'


@dataclass
class Analysis:
    parse: ParseResult
    src: str
    symbols: dict = field(default_factory=dict)
    order: list = field(default_factory=list)
    graph: dict = field(default_factory=dict)
    users: dict = field(default_factory=dict)
    declarations: list = field(default_factory=list)
    metas: list = field(default_factory=list)
    #: names apparently bound by a statement the parser could not read.
    #: They are not real symbols - nothing is known about them - but any
    #: contribution that reads one is standing on unmodelled ground.
    opaque_defs: dict = field(default_factory=dict)
    selections: list = field(default_factory=list)
    energy_stmt: Node | None = None
    energy_expr: Node | None = None
    contributions: list = field(default_factory=list)
    reachable: set = field(default_factory=set)
    problems: list = field(default_factory=list)
    unknown_functions: dict = field(default_factory=dict)
    release: str = ""

    # -- convenience for the GUI and the CLI
    @property
    def opaque_nodes(self) -> list:
        return self.parse.opaque_nodes

    @property
    def opaque_reaching_energy(self) -> list:
        return [c.node for c in self.contributions if c.opaque]

    @property
    def meta_declarations(self) -> list:
        return [d for d in self.declarations if d.which == "declare_meta"]

    def symbol_at(self, offset: int) -> Symbol | None:
        for s in self.symbols.values():
            if s.bind.start <= offset < s.bind.end:
                return s
        return None

    def export_is_lossy(self) -> bool:
        """True when a structured rewrite could drop real physics.

        Three ways that happens: an unreadable span feeds the potential, a
        name feeding it has no definition this analysis can see, or the final
        statement itself could not be parsed.
        """
        if self.opaque_reaching_energy:
            return True
        if self.energy_stmt is not None and \
                self.energy_stmt.kind == cst.OPAQUE:
            return True
        return any(c.opaque for c in self.contributions)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _literal_number(n: Node) -> float | None:
    """The numeric value of a node, if it is a literal (possibly signed)."""
    if n.kind == cst.NUM:
        try:
            return float(n.text)
        except ValueError:
            return None
    if n.kind == cst.PAREN:
        return _literal_number(n.children[0])
    if n.kind == cst.UNARY and n.children:
        v = _literal_number(n.children[0])
        if v is None:
            return None
        return -v if n.op == "-" else v
    return None


def _array_items(n: Node) -> list | None:
    """The elements of an ``array(...)`` call, or None if it is not one.

    ``array`` concatenates, so a nested ``array(h, array(a,b,c))`` is a
    four-element array, not a two-element one.  Counting the top-level
    arguments would misreport the dimensionality of every hill array written
    that way, so nested arrays are spliced in.
    """
    while n.kind == cst.PAREN:
        n = n.children[0]
    if not (n.kind == cst.CALL and n.name == "array"):
        return None
    out: list = []
    for c in n.children:
        nested = _array_items(c)
        if nested is None:
            out.append(c)
        else:
            out.extend(nested)
    return out


def _free_names(n: Node, bound: set) -> set:
    """Names read by a subtree, excluding those bound inside it."""
    out: set = set()

    def walk(node: Node, blocked: set) -> None:
        if node.kind == cst.NAME:
            if node.name not in blocked:
                out.add(node.name)
            return
        if node.kind == cst.BLOCK:
            inner = set(blocked)
            for st in node.children:
                if st.kind == cst.BIND:
                    for c in st.children:
                        walk(c, inner)
                    inner.add(st.name)
                else:
                    for c in st.children:
                        walk(c, inner)
            return
        if node.kind == cst.SERIES:
            inner = set(blocked)
            iters = [c for c in node.children if c.kind == cst.ITER]
            for it in iters:
                for c in it.children:
                    walk(c, inner)
                inner.add(it.name)
            for c in node.children:
                if c.kind != cst.ITER:
                    walk(c, inner)
            return
        for c in node.children:
            walk(c, blocked)

    walk(n, set(bound))
    return out


def _flatten_sum(n: Node) -> list:
    """Additive terms of an expression, keeping subtraction as a term.

    Iterative for the same reason :meth:`Node.walk` is: the chain this walks
    is exactly the one that gets long.
    """
    out: list = []
    stack = [n]
    while stack:
        node = stack.pop()
        while node.kind == cst.PAREN:
            node = node.children[0]
        if node.kind == cst.BINARY and node.op in ("+", "-"):
            stack.append(node.children[1])
            stack.append(node.children[0])
            continue
        out.append(node)
    return out


# --------------------------------------------------------------------------
# the analysis
# --------------------------------------------------------------------------
#: how deep a tree the recursive passes will be given headroom for.  Beyond
#: this the analysis declines rather than risking the interpreter's stack.
MAX_TREE_DEPTH = 20_000


def analyse(res: ParseResult, *, release: str | None = None,
            topology=None, max_dependency_depth: int = 512) -> Analysis:
    """Build the semantic view of a parsed potential.

    A few passes here recurse over the expression tree.  The parser's own
    depth guard counts *its* recursion, which a left-associative chain never
    increases: ``a+b+c+...`` is consumed by one loop and still yields a tree
    as deep as the sum is long.  So the tree's real depth is measured and
    CPython's limit is raised to match for the duration, exactly as
    :func:`funnelforge.core.lang.cst.parse` does, and restored afterwards.
    A tree deeper than :data:`MAX_TREE_DEPTH` is reported rather than
    attempted.
    """
    import sys
    src = res.src
    a = Analysis(parse=res, src=src, release=release or reg.DEFAULT_RELEASE)
    a.problems = list(res.problems)

    depth = res.program.tree_depth()
    if depth > MAX_TREE_DEPTH:
        a.problems.append(Problem(
            "SYN010",
            f"this file's expression tree is {depth} levels deep, past the "
            f"{MAX_TREE_DEPTH} this analysis will attempt; the text is intact "
            "and can still be saved, but the structured view is incomplete",
            res.program.start, res.program.end))
        return a

    prev = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(max(prev, 1000 + depth * 8))
        _collect_declarations(a)
        _collect_opaque_defs(a)
        _collect_symbols(a)
        _infer_types(a, topology)
        _collect_metas(a)
        _find_energy(a)
        _reachability(a, max_dependency_depth)
        _classify_metas(a)
        _check_functions(a)
        _check_declarations(a)
    finally:
        sys.setrecursionlimit(prev)
    return a


def _collect_declarations(a: Analysis) -> None:
    meta_i = 0
    for st in a.parse.program.children:
        if st.kind not in (cst.DECL_META, cst.DECL_OUTPUT):
            continue
        which = "declare_meta" if st.kind == cst.DECL_META else "declare_output"
        terms = {kw.name: kw.children[0] for kw in st.children
                 if kw.kind == cst.KWARG and kw.children}
        d = Declaration(st, which, terms,
                        index=meta_i if which == "declare_meta" else 0)
        if which == "declare_meta":
            meta_i += 1
        a.declarations.append(d)


def _collect_opaque_defs(a: Analysis) -> None:
    """Names an unreadable statement looks like it binds.

    ``mystery @@ from the future;`` is opaque, but if it starts with
    ``name =`` then ``name`` exists as far as the rest of the file is
    concerned.  Recording that keeps two things honest: the name is not
    reported as undefined, and anything that reads it is known to depend on
    something this interface cannot model.
    """
    toks = a.parse.tokens
    for node in a.parse.opaque_nodes:
        code = [t for t in toks[node.ti:node.tj] if not t.is_trivia]
        if len(code) >= 2 and code[0].kind == "ident" and code[1].text == "=":
            a.opaque_defs[code[0].text] = node


def _walk_statements(node: Node, scope: str, out: list) -> None:
    """Yield (statement, scope-id) for the program and every nested block."""
    for st in node.children:
        out.append((st, scope))
        for sub in st.walk():
            if sub.kind == cst.BLOCK:
                _walk_statements(sub, f"{scope}/block@{sub.start}", out)
            elif sub.kind == cst.SERIES:
                pass


def _collect_symbols(a: Analysis) -> None:
    from .lexer import line_starts, position
    starts = line_starts(a.src)

    seen_in_scope: dict = {}
    stmts: list = []
    _walk_statements(a.parse.program, "program", stmts)

    for st, scope in stmts:
        if st.kind != cst.BIND:
            continue
        line, col = position(a.src, st.start, starts)
        sym = Symbol(st.name, st, st.children[0], scope,
                     _line=line, _col=col)
        key = (scope, st.name)
        if key in seen_in_scope:
            prev = seen_in_scope[key]
            a.problems.append(Problem(
                "SEM002",
                f"`{st.name}` is assigned again; Desmond allows a single "
                f"assignment per name in a scope (first at line "
                f"{prev.line})", st.start, st.end,
                evidence=st.span_text(a.src)[:120]))
        seen_in_scope[key] = sym
        # only program-scope names take part in the global graph; a name bound
        # inside a block is local and cannot be referred to from outside it
        if scope == "program":
            a.symbols[st.name] = sym
            a.order.append(st.name)

    # dependencies and uses
    for name, sym in a.symbols.items():
        sym.deps = _free_names(sym.value, set())
        a.graph[name] = set(sym.deps)
    for name in a.symbols:
        a.users[name] = set()
    for name, sym in a.symbols.items():
        for d in sym.deps:
            if d in a.users:
                a.users[d].add(name)

    # every NAME node that resolves to a program symbol
    for node in a.parse.program.walk():
        if node.kind == cst.NAME and node.name in a.symbols:
            a.symbols[node.name].uses.append(node)

    # Undefined names.  This walks only the *top-level* statements:
    # _free_names already accounts for every nested scope, so a name bound
    # inside a block or by a series iterator is invisible here, which is
    # exactly right - it is not a program-scope name and it is not undefined
    # either.
    defined = set(a.symbols) | set(a.opaque_defs)
    for st in a.parse.program.children:
        if st.kind == cst.STATIC:
            for entry in st.children:
                defined.add(entry.name)
    for st in a.parse.program.children:
        if st.kind in (cst.DECL_META, cst.DECL_OUTPUT, cst.STATIC,
                       cst.OPAQUE):
            continue
        for nm in _free_names(st, set()):
            if nm not in defined:
                node = next((x for x in st.walk()
                             if x.kind == cst.NAME and x.name == nm), st)
                a.problems.append(Problem(
                    "SEM001", f"`{nm}` is used but never defined",
                    node.start, node.end, evidence=nm))
                defined.add(nm)      # report once

    _detect_cycles(a)


def _detect_cycles(a: Analysis) -> None:
    colour: dict = {}
    stack: list = []

    def visit(n: str) -> None:
        colour[n] = 1
        stack.append(n)
        for d in sorted(a.graph.get(n, ())):
            if d not in a.graph:
                continue
            if colour.get(d, 0) == 0:
                visit(d)
            elif colour.get(d) == 1:
                cyc = stack[stack.index(d):] + [d]
                sym = a.symbols[n]
                a.problems.append(Problem(
                    "SEM003",
                    "definitions depend on each other in a cycle: "
                    + " -> ".join(cyc), sym.bind.start, sym.bind.end,
                    evidence=" -> ".join(cyc)))
        stack.pop()
        colour[n] = 2

    for n in list(a.graph):
        if colour.get(n, 0) == 0:
            visit(n)


def _infer_types(a: Analysis, topology) -> None:
    sigs = reg.signatures(a.release)
    memo: dict = {}

    def type_of(node: Node, env: dict) -> Type:
        key = id(node)
        if key in memo:
            return memo[key]
        t = _type_of(node, env)
        memo[key] = t
        return t

    def _type_of(node: Node, env: dict) -> Type:
        k = node.kind
        if k == cst.NUM:
            return T_SCALAR
        if k == cst.STR:
            return T_STRING
        if k == cst.PAREN:
            return type_of(node.children[0], env)
        if k == cst.NAME:
            if node.name in env:
                return env[node.name]
            sym = a.symbols.get(node.name)
            if sym is not None and sym.type is not T_UNKNOWN:
                return sym.type
            return T_UNKNOWN
        if k == cst.UNARY:
            return type_of(node.children[0], env)
        if k == cst.BINARY:
            ta = type_of(node.children[0], env)
            tb = type_of(node.children[1], env)
            if ta.cat == SELECTION or tb.cat == SELECTION:
                return Type(UNKNOWN, None, "arithmetic on a selection")
            if ta.dim is None or tb.dim is None:
                return Type(NUMBER, None)
            if ta.dim == tb.dim:
                return Type(NUMBER, ta.dim)
            if ta.dim == 1 or tb.dim == 1:
                return Type(NUMBER, max(ta.dim, tb.dim))
            return Type(NUMBER, None, "operands of different lengths")
        if k == cst.IFELSE:
            tb = type_of(node.children[1], env)
            tc = type_of(node.children[2], env)
            if tb.dim is not None and tc.dim is not None and tb.dim != tc.dim:
                return Type(NUMBER, None, "branches of different lengths")
            return tb if tb.dim is not None else tc
        if k == cst.BLOCK:
            inner = dict(env)
            last = T_UNKNOWN
            for st in node.children:
                if st.kind == cst.BIND:
                    inner[st.name] = type_of(st.children[0], inner)
                elif st.children:
                    last = type_of(st.children[0], inner)
            return last
        if k == cst.SERIES:
            inner = dict(env)
            for it in node.children:
                if it.kind == cst.ITER:
                    inner[it.name] = T_SCALAR
            body = [c for c in node.children if c.kind != cst.ITER]
            return type_of(body[-1], inner) if body else T_UNKNOWN
        if k == cst.INDEX:
            base = type_of(node.children[0], env)
            if base.cat == SELECTION:
                return Type(SELECTION, 1)
            return T_SCALAR
        if k == cst.CALL:
            return _call_type(node, env)
        return T_UNKNOWN

    def _call_type(node: Node, env: dict) -> Type:
        name = node.name
        if name == "atomsel":
            n_at = None
            if topology is not None and node.children:
                arg = node.children[0]
                if arg.kind == cst.STR:
                    n_at = _resolve_count(topology, arg.text.strip('"'))
            return Type(SELECTION, n_at)
        if name == "array":
            total = 0
            for c in node.children:
                t = type_of(c, env)
                if t.dim is None:
                    return Type(NUMBER, None)
                total += t.dim
            return Type(NUMBER, total)
        if name == "meta":
            return T_SCALAR
        sig = sigs.get(name)
        if sig is None:
            return Type(UNKNOWN, None, f"`{name}` is not in the registry")
        if sig.kind in ("thread", "binary_thread"):
            return type_of(node.children[0], env) if node.children \
                else T_UNKNOWN
        ret = sig.ret
        if isinstance(ret, int) and ret >= 0:
            return Type(NUMBER, ret)
        if isinstance(ret, int) and ret < 0:
            # a wildcard: the same dimensionality as the matching argument
            for code, arg in zip(sig.args, node.children):
                if code == ret:
                    return type_of(arg, env)
            return Type(NUMBER, None)
        if ret == "string":
            return T_STRING
        return T_UNKNOWN

    for name in a.order:
        sym = a.symbols[name]
        sym.type = type_of(sym.value, {})
        if sym.type.cat == SELECTION:
            a.selections.append(sym)


def _resolve_count(topology, asl: str):
    """How many atoms an ASL string selects, or None when it cannot be run."""
    try:
        from ..asl import parse_selection
        idx = parse_selection(asl, topology)
        return len(idx)
    except Exception:
        return None


def resolve_array(a: Analysis, node: Node, _depth: int = 0) -> list | None:
    """The elements of an array expression, following single-name aliases.

    ``meta(0, probe_widths, cv_pair)`` is as ordinary as writing the arrays
    inline, so the hill and CV arrays are resolved through the symbol table
    before they are counted or tested.  Without this a perfectly normal
    well-tempered file reads as plain metadynamics.
    """
    if node is None or _depth > 32:
        return None
    n = node
    while n.kind == cst.PAREN:
        n = n.children[0]
    items = _array_items(n)
    if items is not None:
        out: list = []
        for it in items:
            nested = resolve_array(a, it, _depth + 1)
            if nested is None:
                out.append(it)
            else:
                out.extend(nested)
        return out
    if n.kind == cst.NAME:
        sym = a.symbols.get(n.name)
        if sym is not None:
            return resolve_array(a, sym.value, _depth + 1)
    return None


def _collect_metas(a: Analysis) -> None:
    """Every meta() call, at any depth, bound to a name or not."""
    owner_of: dict = {}
    for name, sym in a.symbols.items():
        for n in sym.value.walk():
            if n.kind == cst.CALL and n.name == "meta":
                owner_of[id(n)] = name

    for node in a.parse.program.walk():
        if node.kind != cst.CALL or node.name != "meta":
            continue
        args = node.children
        idx_node = args[0] if len(args) > 0 else None
        hills = args[1] if len(args) > 1 else None
        cvs = args[2] if len(args) > 2 else None
        idx_val = _literal_number(idx_node) if idx_node is not None else None
        hills_items = resolve_array(a, hills)
        cv_items = resolve_array(a, cvs)
        zero = False
        if hills_items:
            vals = [const_value(a, x) for x in hills_items]
            zero = all(v is not None and v == 0.0 for v in vals)
        elif hills is not None:
            v = const_value(a, hills)
            zero = v is not None and v == 0.0
        mc = MetaCall(
            node=node,
            index=int(idx_val) if idx_val is not None else None,
            index_node=idx_node,
            hills=hills, cvs=cvs,
            n_hills=len(hills_items) if hills_items is not None else None,
            n_cvs=len(cv_items) if cv_items is not None else None,
            zero_height=zero,
            owner=owner_of.get(id(node), ""),
            depth=node.depth(),
        )
        decls = a.meta_declarations
        if mc.index is not None and 0 <= mc.index < len(decls):
            mc.declared = decls[mc.index]
        a.metas.append(mc)


def _find_energy(a: Analysis) -> None:
    """The potential is the file's last expression statement."""
    for st in reversed(a.parse.program.children):
        if st.kind == cst.EXPR_STMT:
            a.energy_stmt = st
            a.energy_expr = st.children[0] if st.children else None
            return
        if st.kind == cst.OPAQUE:
            a.energy_stmt = st
            a.energy_expr = None
            return


def _reachability(a: Analysis, max_depth: int) -> None:
    """Which definitions actually reach the potential or a side effect."""
    sigs = reg.signatures(a.release)

    def closure(names: set) -> set:
        seen: set = set()
        stack = list(names)
        depth = 0
        while stack:
            depth += 1
            if depth > max_depth * 8:
                a.problems.append(Problem(
                    "SEM004", "dependency walk exceeded its depth limit",
                    0, 0))
                break
            n = stack.pop()
            if n in seen or n not in a.symbols:
                continue
            seen.add(n)
            stack.extend(a.graph.get(n, ()))
        return seen

    energy_names: set = set()
    if a.energy_expr is not None:
        energy_names = _free_names(a.energy_expr, set())
    a.reachable = closure(energy_names)
    for n in a.reachable:
        a.symbols[n].reaches_energy = True

    # side-effecting roots: print / store / meta / declare_*
    side: set = set()
    for node in a.parse.program.walk():
        if node.kind == cst.CALL and reg.is_side_effecting(node.name,
                                                           a.release):
            for c in node.children:
                side |= _free_names(c, set())
    for n in closure(side):
        a.symbols[n].reaches_side_effect = True

    # The additive contributions of the final energy.  A file almost always
    # ends with one name, so the sum is expanded through single-name
    # bindings until real terms appear - otherwise the interface would show
    # "v_total" and claim to have listed every contribution.
    if a.energy_expr is not None:
        for term, via in _expand_contributions(a, a.energy_expr):
            sym = a.symbols.get(term.name) if term.kind == cst.NAME else None
            kinds: set = set()
            probe = sym.value if sym is not None else term
            for n in probe.walk():
                if n.kind == cst.CALL and n.name == "meta":
                    kinds.add("meta")
                if n.kind == cst.IFELSE:
                    kinds.add("wall")
            opaque = _touches_opaque(a, term, sym)
            label = term.span_text(a.src).strip()
            if via:
                label = f"{label}  (via {' -> '.join(via)})"
            a.contributions.append(
                Contribution(term, label, sym, opaque=opaque, kinds=kinds))
    elif a.energy_stmt is not None:
        a.contributions.append(Contribution(
            a.energy_stmt, a.energy_stmt.span_text(a.src).strip(), None,
            opaque=True, kinds={"unknown"}))


def _expand_contributions(a: Analysis, expr: Node, _depth: int = 0,
                          _via: tuple = ()) -> list:
    """Additive terms of the potential, following single-name aliases.

    ``v_total;`` on its own says nothing, so a term that is just a name whose
    definition is itself a sum is replaced by that sum's terms.  Expansion
    stops at anything that is not a plain alias, so a genuine single term is
    reported as itself, and the chain it was reached through is recorded so
    the user can see where it came from.
    """
    out: list = []
    for term in _flatten_sum(expr):
        t = term
        while t.kind == cst.PAREN:
            t = t.children[0]
        if t.kind == cst.NAME and _depth < 8:
            sym = a.symbols.get(t.name)
            if sym is not None:
                parts = _flatten_sum(sym.value)
                if len(parts) > 1:
                    out.extend(_expand_contributions(
                        a, sym.value, _depth + 1, _via + (t.name,)))
                    continue
        out.append((term, _via))
    return out


def _touches_opaque(a: Analysis, term: Node, sym: Symbol | None) -> bool:
    """Does this contribution depend on anything the parser could not read?"""
    opaque_spans = [(n.start, n.end) for n in a.parse.opaque_nodes]
    if not opaque_spans:
        return False
    names = _free_names(term, set())
    stack = list(names)
    seen: set = set()
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if n in a.opaque_defs:
            return True
        s = a.symbols.get(n)
        if s is None:
            # a name that reaches the potential but has no definition this
            # analysis can see: whatever it contributes is unmodelled, so a
            # structured rewrite could not reproduce it
            return True
        for lo, hi in opaque_spans:
            if s.bind.start <= lo and hi <= s.bind.end:
                return True
        stack.extend(a.graph.get(n, ()))
    for lo, hi in opaque_spans:
        if term.start <= lo and hi <= term.end:
            return True
    return False


def _expr_closure(a: Analysis, node: Node) -> list:
    """All CST nodes reachable from an expression, following definitions."""
    out: list = list(node.walk())
    seen: set = set()
    stack = list(_free_names(node, set()))
    while stack:
        n = stack.pop()
        if n in seen or n not in a.symbols:
            continue
        seen.add(n)
        sym = a.symbols[n]
        out.extend(sym.value.walk())
        stack.extend(a.graph.get(n, ()))
    return out


def _classify_metas(a: Analysis) -> None:
    """Assign each meta() call a role and look for the well-tempered relation."""
    energy_closure_ids = set()
    if a.energy_expr is not None:
        energy_closure_ids = {id(n) for n in _expr_closure(a, a.energy_expr)}

    printed_ids: set = set()
    for node in a.parse.program.walk():
        if node.kind == cst.CALL and node.name == "print":
            for c in node.children:
                printed_ids |= {id(n) for n in _expr_closure(a, c)}

    for mc in a.metas:
        if mc.zero_height:
            mc.role = PROBE
        elif id(mc.node) in energy_closure_ids:
            mc.role = BIAS
        elif id(mc.node) in printed_ids:
            mc.role = DIAGNOSTIC
        elif mc.owner and a.symbols.get(mc.owner) is not None and \
                a.symbols[mc.owner].uses:
            mc.role = INTERMEDIATE
        else:
            mc.role = UNDETERMINED

    for mc in a.metas:
        if mc.role == PROBE or mc.hills is None:
            continue
        items = resolve_array(a, mc.hills)
        height = items[0] if items else mc.hills
        mc.wt = _well_tempered(a, mc, height)


def _well_tempered(a: Analysis, mc: MetaCall, height: Node) -> WellTempered:
    """Is this hill height h0*exp(-V/kDT) with V read back from the same well?

    The test is on the data flow, not on the text: the height's dependency
    closure must contain an ``exp`` whose *argument* closure contains a
    zero-height ``meta`` call on the same accumulator, and that argument must
    be negated somewhere.  Spreading the algebra over intermediate
    assignments therefore makes no difference.
    """
    closure = _expr_closure(a, height)
    exps = [n for n in closure if n.kind == cst.CALL and n.name == "exp"]
    if not exps:
        return WellTempered(False, None, None, False, None, None,
                            "no exp() in the hill height's data flow")
    for e in exps:
        if not e.children:
            continue
        arg = e.children[0]
        arg_nodes = _expr_closure(a, arg)
        probe = None
        for n in arg_nodes:
            if n.kind == cst.CALL and n.name == "meta":
                p = next((m for m in a.metas if m.node is n), None)
                if p is not None and p.zero_height and \
                        (p.index == mc.index or mc.index is None):
                    probe = p
                    break
        if probe is None:
            continue
        sign = coefficient_sign(a, arg, probe.node)
        negated = sign is not None and sign < 0
        ktemp = _ktemp_of(a, arg, probe.node)
        h0 = _leading_factor(a, height)
        if negated:
            ev = ("the hill height reads accumulator "
                  f"{probe.index} back through exp() with a negative "
                  "coefficient")
            if ktemp is not None:
                ev += f"; kDT = {ktemp:.6g} kcal/mol"
            if h0 is not None:
                ev += f"; h0 = {h0:.6g} kcal/mol"
        elif sign is None:
            ev = ("the accumulated bias reaches the exponent, but the sign "
                  "of its coefficient could not be established from the "
                  "data flow")
        else:
            ev = ("the accumulated bias enters the exponent with a positive "
                  "coefficient, which is not the well-tempered relation")
        return WellTempered(negated, probe, e, negated, ktemp, h0, ev)
    return WellTempered(False, None, exps[0], False, None, None,
                        "an exp() is present but it does not read this "
                        "accumulator's bias back")


def _ktemp_of(a: "Analysis", arg: Node, target: Node, _depth: int = 0):
    """|kDT| from an exponent of the form V/(-kDT) or (-1/kDT)*V."""
    n = arg
    while n.kind == cst.PAREN:
        n = n.children[0]
    if n.kind == cst.NAME:
        sym = a.symbols.get(n.name)
        if sym is not None and _depth < 64:
            return _ktemp_of(a, sym.value, target, _depth + 1)
        return None
    if n.kind == cst.UNARY and _depth < 64:
        return _ktemp_of(a, n.children[0], target, _depth + 1)
    if n.kind == cst.BINARY and n.op in ("*", "/"):
        left, right = n.children
        in_left = _contains(a, left, target)
        other = right if in_left else left
        c = const_value(a, other)
        if c is None or c == 0:
            return None
        return abs(c) if n.op == "/" else abs(1.0 / c)
    return None


def const_value(a: "Analysis", node: Node, _depth: int = 0):
    """Fold an expression to a number when every leaf is a constant.

    Names are resolved through the symbol table, so a bias factor spread over
    ``kb``, ``T``, ``gamma-1`` and a final product still folds to one number.
    Returns None as soon as anything runtime-dependent is met.
    """
    if _depth > 64:
        return None
    n = node
    while n.kind == cst.PAREN:
        n = n.children[0]
    if n.kind == cst.NUM:
        try:
            return float(n.text)
        except ValueError:
            return None
    if n.kind == cst.UNARY:
        v = const_value(a, n.children[0], _depth + 1)
        if v is None:
            return None
        return -v if n.op == "-" else v
    if n.kind == cst.NAME:
        sym = a.symbols.get(n.name)
        if sym is None:
            return None
        return const_value(a, sym.value, _depth + 1)
    if n.kind == cst.BINARY:
        x = const_value(a, n.children[0], _depth + 1)
        y = const_value(a, n.children[1], _depth + 1)
        if x is None or y is None:
            return None
        if n.op == "+":
            return x + y
        if n.op == "-":
            return x - y
        if n.op == "*":
            return x * y
        if n.op == "/":
            return x / y if y != 0 else None
        if n.op == "^":
            try:
                return x ** y
            except (ValueError, OverflowError):
                return None
    return None


def _contains(a: "Analysis", node: Node, target: Node, _seen=None) -> bool:
    """Does ``node``'s data flow reach ``target`` (a specific CST node)?"""
    _seen = set() if _seen is None else _seen
    for n in node.walk():
        if n is target:
            return True
    for nm in _free_names(node, set()):
        if nm in _seen:
            continue
        _seen.add(nm)
        sym = a.symbols.get(nm)
        if sym is not None and _contains(a, sym.value, target, _seen):
            return True
    return False


def coefficient_sign(a: "Analysis", node: Node, target: Node,
                     _depth: int = 0):
    """Sign of the coefficient multiplying ``target`` inside ``node``.

    This is what actually decides whether a hill height is *well tempered*:
    the accumulated bias has to enter the exponent with a negative
    coefficient.  Working it out structurally means the minus sign may live
    anywhere - a unary minus, a negative literal, ``0.0 - dT``, a division by
    a folded-negative constant, or several assignments away.

    Returns +1, -1, or None when it cannot be established (for instance when
    the target appears in a denominator, which is not a linear coefficient).
    """
    if _depth > 64:
        return None
    n = node
    while n.kind == cst.PAREN:
        n = n.children[0]
    if n is target:
        return 1
    if n.kind == cst.NAME:
        sym = a.symbols.get(n.name)
        if sym is None:
            return None
        return coefficient_sign(a, sym.value, target, _depth + 1)
    if n.kind == cst.UNARY:
        inner = coefficient_sign(a, n.children[0], target, _depth + 1)
        if inner is None:
            return None
        return -inner if n.op == "-" else inner
    if n.kind == cst.BINARY:
        left, right = n.children
        in_left = _contains(a, left, target)
        in_right = _contains(a, right, target)
        if in_left and in_right:
            return None
        if n.op in ("+", "-"):
            if in_left:
                return coefficient_sign(a, left, target, _depth + 1)
            if in_right:
                s = coefficient_sign(a, right, target, _depth + 1)
                if s is None:
                    return None
                return -s if n.op == "-" else s
            return None
        if n.op in ("*", "/"):
            if in_right and n.op == "/":
                return None            # target in a denominator: not linear
            side = left if in_left else right
            other = right if in_left else left
            s = coefficient_sign(a, side, target, _depth + 1)
            c = const_value(a, other)
            if s is None or c is None or c == 0:
                return None
            return s if c > 0 else -s
        return None
    if _contains(a, n, target):
        return None
    return None


def _leading_factor(a: Analysis, height: Node) -> float | None:
    """h0 when the height is written as h0*exp(...)."""
    n = height
    while n.kind == cst.PAREN:
        n = n.children[0]
    if n.kind == cst.NAME and n.name in a.symbols:
        n = a.symbols[n.name].value
        while n.kind == cst.PAREN:
            n = n.children[0]
    if n.kind == cst.BINARY and n.op == "*":
        for side in n.children:
            v = _literal_number(side)
            if v is not None:
                return v
            if side.kind == cst.NAME and side.name in a.symbols:
                v = _literal_number(a.symbols[side.name].value)
                if v is not None:
                    return v
    return None


def _check_functions(a: Analysis) -> None:
    """Record calls the registry does not know - preserved, never rejected."""
    for node in a.parse.program.walk():
        if node.kind != cst.CALL:
            continue
        sig = reg.get(node.name, a.release)
        if sig is None:
            a.unknown_functions.setdefault(node.name, []).append(node)
            continue
        if sig.nargs and len(node.children) not in sig.nargs:
            a.problems.append(Problem(
                "SEM010",
                f"`{node.name}()` takes "
                + " or ".join(str(x) for x in sig.nargs)
                + f" arguments, {len(node.children)} given",
                node.start, node.end,
                evidence=node.span_text(a.src)[:120]))


def _check_declarations(a: Analysis) -> None:
    decls = a.meta_declarations
    for mc in a.metas:
        if mc.index is None:
            a.problems.append(Problem(
                "MTD002", "the accumulator index of this meta() call is not "
                          "a literal, so it cannot be checked",
                mc.node.start, mc.node.end))
            continue
        if not decls:
            a.problems.append(Problem(
                "MTD003", f"meta({mc.index}, ...) is used but the file has no "
                          "declare_meta", mc.node.start, mc.node.end))
            continue
        if not (0 <= mc.index < len(decls)):
            a.problems.append(Problem(
                "MTD003",
                f"meta({mc.index}, ...) refers to accumulator {mc.index} but "
                f"the file declares {len(decls)}",
                mc.node.start, mc.node.end))
            continue
        dim = decls[mc.index].dimension
        if dim is not None and mc.n_cvs is not None and mc.n_cvs != dim:
            a.problems.append(Problem(
                "MTD004",
                f"declare_meta says dimension = {dim} but this call passes "
                f"{mc.n_cvs} collective variable(s)",
                mc.node.start, mc.node.end,
                evidence=mc.cvs.span_text(a.src) if mc.cvs else ""))
        if dim is not None and mc.n_hills is not None and \
                mc.n_hills != dim + 1:
            a.problems.append(Problem(
                "MTD005",
                f"the hill array should hold one height and {dim} width(s), "
                f"that is {dim + 1} values; this call passes {mc.n_hills}",
                mc.node.start, mc.node.end,
                evidence=mc.hills.span_text(a.src) if mc.hills else ""))
    for d in decls:
        dim = d.dimension
        if dim is not None and dim <= 0:
            a.problems.append(Problem(
                "MTD006", f"declare_meta dimension must be positive, not {dim}",
                d.node.start, d.node.end))
