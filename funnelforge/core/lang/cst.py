"""Concrete syntax tree for the Desmond M-expression language.

Every node is a *span over the original text*.  Nothing is copied, normalised
or regenerated: a node knows where it starts and ends, and the source string
remains the single source of truth.  That is what makes a byte-for-byte round
trip trivial rather than aspirational - the file is never rebuilt, only
patched (see :mod:`funnelforge.core.lang.patch`).

The grammar implemented is Desmond's own (``mexp.g``)::

    prog          : header block EOF
    header        : (decl SEMI)*
    decl          : decl_meta | decl_output | static
    static        : 'static' varWithType (COMMA varWithType)*
    varWithType   : IDENT POPEN LIT PCLOSE
    block         : exprOrBind (SEMI exprOrBind)* SEMI?
    exprOrBind    : expr | bind
    bind          : IDENT EQ expr
    expr          : factor ( ('+'|'-') factor )*
    factor        : signedExpComp ( ('*'|'/') signedExpComp )*
    signedExpComp : ('+'|'-')? expComp
    expComp       : comp ( '^' signedExpComp )?
    comp          : atom ( '[' expr ']' )*
    atom          : series | if_ | fcnCall | STRING | IDENT | LIT
                  | '(' expr ')' | '{' block '}'
    series        : 'series' '(' iter (COMMA iter)* ')' expr
    iter          : IDENT '=' expr ':' expr
    if_           : 'if' expr 'then' expr 'else' expr

Two precedence details follow from those rules and are easy to get backwards:
unary sign binds **looser** than ``^`` (so ``-x^2`` is ``-(x^2)``), and ``^``
is right-associative with a possibly-signed exponent (so ``2^-3`` parses).

Recovery, not rejection
-----------------------
A construct this parser cannot explain does not abort the parse and is never
discarded.  It becomes an :data:`OPAQUE` node carrying its exact span, and a
problem is recorded.  A future Desmond release can therefore add syntax and
this editor will still open, display and losslessly save the file - while
:mod:`funnelforge.core.lang.sema` refuses to let a structured export happen if
an opaque node contributes to the potential.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lexer import (EOF_, IDENT, KEYWORD, NUMBER, OP, RESERVED, STRING,
                    Token, tokenize)

# -- node kinds -------------------------------------------------------------
PROGRAM = "program"
#: mexp.g gives each header form a closed set of terms; anything else is
#: "no viable alternative at input '<key>'" from the engine.
DECL_TERMS = {
    "declare_meta": ("name", "first", "interval", "cutoff", "dimension",
                     "initial"),
    "declare_output": ("name", "first", "interval"),
}

DECL_META = "decl_meta"
DECL_OUTPUT = "decl_output"
STATIC = "static"
STATIC_VAR = "static_var"
BIND = "bind"
EXPR_STMT = "expr_stmt"
OPAQUE = "opaque"

NUM = "num"
STR = "str"
NAME = "name"
CALL = "call"
INDEX = "index"
UNARY = "unary"
BINARY = "binary"
IFELSE = "ifelse"
SERIES = "series"
ITER = "iter"
BLOCK = "block"
PAREN = "paren"
KWARG = "kwarg"

#: statement-level kinds, i.e. the direct children of a program or block
STATEMENT_KINDS = frozenset({DECL_META, DECL_OUTPUT, STATIC, BIND, EXPR_STMT,
                             OPAQUE})


@dataclass
class Node:
    """One span of source with a syntactic interpretation.

    ``start``/``end`` are character offsets into the source text; ``ti``/``tj``
    are the half-open token-index range, which is what patch-based editing
    uses to find the exact tokens to replace.
    """

    kind: str
    start: int
    end: int
    ti: int = 0
    tj: int = 0
    children: list = field(default_factory=list)
    name: str = ""          # bind target, call callee, identifier, kwarg key
    op: str = ""            # operator text for unary/binary
    text: str = ""          # raw lexeme for num/str
    parent: "Node | None" = field(default=None, repr=False, compare=False)

    # -- navigation
    def walk(self):
        """Every node of this subtree, iteratively.

        A left-associative chain such as ``a + b + c + ... + z`` produces a
        tree as deep as it is long, and a 500-term sum is ordinary in a
        potential that adds up many walls.  Recursing here would hit
        CPython's own limit long before any of this module's limits, so the
        traversal keeps its own stack.  Children are pushed in reverse so the
        order matches the recursive version exactly.
        """
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def tree_depth(self) -> int:
        """Deepest path below this node, counted iteratively."""
        best = 0
        stack = [(self, 1)]
        while stack:
            node, d = stack.pop()
            if d > best:
                best = d
            for c in node.children:
                stack.append((c, d + 1))
        return best

    def find(self, kind: str):
        return [n for n in self.walk() if n.kind == kind]

    def span_text(self, src: str) -> str:
        return src[self.start:self.end]

    def depth(self) -> int:
        d, p = 0, self.parent
        while p is not None:
            d += 1
            p = p.parent
        return d

    def __repr__(self) -> str:      # pragma: no cover - debugging aid
        tag = self.name or self.op or self.text
        return f"<{self.kind}{':' + tag if tag else ''} {self.start}-{self.end}>"


@dataclass
class Problem:
    """A syntax-layer finding, kept independent of the diagnostics catalogue.

    Keeping this dataclass local means the parser has no import-time
    dependency on the diagnostics module; :mod:`funnelforge.core.lang.glue`
    turns these into catalogued :class:`Diagnostic` objects.
    """

    code: str
    message: str
    start: int
    end: int
    evidence: str = ""


@dataclass
class ParseResult:
    src: str
    program: Node
    tokens: list
    problems: list

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def opaque_nodes(self) -> list:
        return self.program.find(OPAQUE)

    def statements(self) -> list:
        return list(self.program.children)


class _Parser:
    """Recursive descent over the non-trivia tokens, recovering at ``;``."""

    def __init__(self, src: str, tokens: list, max_depth: int = 256):
        self.src = src
        self.toks = tokens
        self.max_depth = max_depth
        self.problems: list = []
        #: indices of the non-trivia tokens, in order
        self.code = [i for i, t in enumerate(tokens)
                     if not t.is_trivia and t.kind != EOF_]
        self.p = 0                      # position within self.code
        self.depth = 0

    # -- token helpers
    def _tok(self, k: int = 0) -> Token:
        j = self.p + k
        if j >= len(self.code):
            return self.toks[-1]        # the EOF token
        return self.toks[self.code[j]]

    def _ti(self, k: int = 0) -> int:
        j = self.p + k
        return self.code[j] if j < len(self.code) else len(self.toks) - 1

    @property
    def at_end(self) -> bool:
        return self.p >= len(self.code)

    def _advance(self) -> Token:
        t = self._tok()
        self.p += 1
        return t

    def _is(self, text: str, k: int = 0) -> bool:
        return self._tok(k).text == text

    def _accept(self, text: str) -> Token | None:
        if self._is(text):
            return self._advance()
        return None

    def _expect(self, text: str, what: str) -> Token:
        if self._is(text):
            return self._advance()
        t = self._tok()
        raise _Recover(f"expected {text!r} {what}, found {_show(t)}",
                       t.start, t.end)

    def _node(self, kind: str, ti: int, **kw) -> Node:
        """Build a node spanning tokens ``ti`` .. the last consumed token."""
        tj = self._ti(-1) if self.p else ti
        last = self.toks[tj]
        first = self.toks[ti]
        n = Node(kind, first.start, last.end, ti, tj + 1, **kw)
        for c in n.children:
            c.parent = n
        return n

    # -- entry point
    def parse(self) -> Node:
        stmts: list = []
        body_started = False
        while not self.at_end:
            before = self.p
            stmt = self._statement()
            if stmt is not None:
                # `prog : header block` - every declare_meta, declare_output
                # and static belongs to the header.  The engine rejects one
                # that appears after the first ordinary statement ("no viable
                # alternative at input 'static'"), so it is reported here
                # rather than being discovered only at run time.
                if stmt.kind in (DECL_META, DECL_OUTPUT, STATIC):
                    if body_started:
                        self.problems.append(Problem(
                            "SYN001",
                            "declarations belong at the top of the file: "
                            "Desmond's grammar is 'header block', so this "
                            "declaration after the first ordinary statement "
                            "will be rejected by the engine",
                            stmt.start, stmt.end,
                            evidence=self.src[stmt.start:stmt.end][:120]))
                elif stmt.kind != OPAQUE:
                    body_started = True
                stmts.append(stmt)
            if self.p == before:        # defensive: never spin
                self.p += 1
        start = self.toks[self.code[0]].start if self.code else 0
        end = self.toks[self.code[-1]].end if self.code else 0
        prog = Node(PROGRAM, start, end, 0, len(self.toks), children=stmts)
        for c in stmts:
            c.parent = prog
        return prog

    # -- statements
    def _statement(self) -> Node | None:
        ti = self._ti()
        mark = self.p
        try:
            node = self._statement_inner()
        except _Recover as exc:
            self.problems.append(Problem("SYN001", exc.message, exc.start,
                                         exc.end))
            return self._opaque_from(mark)
        except RecursionError:
            self.problems.append(Problem(
                "SYN010", "expression nested too deeply to analyse; it is "
                          "preserved verbatim",
                self.toks[ti].start, self._tok().end))
            return self._opaque_from(mark)
        # a statement must be followed by ';' or the end of the file
        if not self.at_end and not self._is(";"):
            t = self._tok()
            self.problems.append(Problem(
                "SYN001", f"expected ';' after this statement, found "
                          f"{t.text!r}", t.start, t.end))
            return self._opaque_from(mark)
        self._accept(";")
        node.end = self.toks[self._ti(-1)].end
        node.tj = self._ti(-1) + 1
        return node

    def _opaque_from(self, mark: int) -> Node:
        """Consume to the end of the statement and keep the text verbatim."""
        self.p = mark
        ti = self._ti()
        depth = 0
        while not self.at_end:
            t = self._tok()
            if t.text in "([{":
                depth += 1
            elif t.text in ")]}":
                depth = max(0, depth - 1)
            elif t.text == ";" and depth == 0:
                self._advance()
                break
            self._advance()
        tj = self._ti(-1)
        first, last = self.toks[ti], self.toks[tj]
        return Node(OPAQUE, first.start, last.end, ti, tj + 1)

    def _statement_inner(self) -> Node:
        t = self._tok()
        if t.kind == KEYWORD and t.text in ("declare_meta", "declare_output"):
            return self._declare(t.text)
        if t.kind == KEYWORD and t.text == "static":
            return self._static()
        if t.kind == IDENT and self._is("=", 1):
            ti = self._ti()
            name = self._advance().text
            if name in RESERVED:
                self.problems.append(Problem(
                    "SYN001",
                    f"`{name}` is a reserved word of the M-expression "
                    "grammar (it is a declaration term), so it cannot be "
                    "used as a variable; Desmond reports 'no viable "
                    f"alternative at input {name!r}'",
                    t.start, t.end, evidence=name))
            self._advance()                     # '='
            value = self._expr()
            n = self._node(BIND, ti, name=name, children=[value])
            value.parent = n
            return n
        ti = self._ti()
        value = self._expr()
        n = self._node(EXPR_STMT, ti, children=[value])
        value.parent = n
        return n

    def _declare(self, which: str) -> Node:
        ti = self._ti()
        self._advance()
        self._expect("(", f"after {which}")
        terms: list = []
        if not self._is(")"):
            while True:
                kti = self._ti()
                key_tok = self._tok()
                if key_tok.kind not in (IDENT, KEYWORD) or not self._is("=", 1):
                    raise _Recover(
                        f"{which} takes 'name = value' terms; found "
                        f"{key_tok.text!r}", key_tok.start, key_tok.end)
                key = self._advance().text
                allowed = DECL_TERMS.get(which, ())
                if key not in allowed:
                    self.problems.append(Problem(
                        "SYN001",
                        f"{which} has no term called {key!r}; the grammar "
                        "allows only " + ", ".join(allowed),
                        key_tok.start, key_tok.end, evidence=key))
                self._advance()                 # '='
                val = self._expr()
                kw = self._node(KWARG, kti, name=key, children=[val])
                val.parent = kw
                terms.append(kw)
                if self._accept(","):
                    continue
                break
        self._expect(")", f"closing {which}")
        kind = DECL_META if which == "declare_meta" else DECL_OUTPUT
        n = self._node(kind, ti, name=which, children=terms)
        for c in terms:
            c.parent = n
        return n

    def _static(self) -> Node:
        ti = self._ti()
        self._advance()
        entries: list = []
        while True:
            vti = self._ti()
            name_tok = self._tok()
            if name_tok.kind != IDENT:
                raise _Recover("static needs a variable name",
                               name_tok.start, name_tok.end)
            name = self._advance().text
            self._expect("(", "after a static variable name")
            size_tok = self._tok()
            if size_tok.kind != NUMBER:
                raise _Recover("a static variable's size must be a literal",
                               size_tok.start, size_tok.end)
            self._advance()
            self._expect(")", "closing a static variable's size")
            entries.append(self._node(STATIC_VAR, vti, name=name,
                                      text=size_tok.text))
            if self._accept(","):
                continue
            break
        n = self._node(STATIC, ti, children=entries)
        for c in entries:
            c.parent = n
        return n

    # -- expressions, following the grammar's precedence exactly
    def _expr(self) -> Node:
        self.depth += 1
        if self.depth > self.max_depth:
            self.depth -= 1
            raise RecursionError("expression nesting limit")
        try:
            ti = self._ti()
            node = self._factor()
            while self._tok().text in ("+", "-") and self._tok().kind == OP:
                op = self._advance().text
                rhs = self._factor()
                node = self._binary(ti, op, node, rhs)
            return node
        finally:
            self.depth -= 1

    def _factor(self) -> Node:
        ti = self._ti()
        node = self._signed()
        while self._tok().text in ("*", "/") and self._tok().kind == OP:
            op = self._advance().text
            rhs = self._signed()
            node = self._binary(ti, op, node, rhs)
        return node

    def _signed(self) -> Node:
        t = self._tok()
        if t.kind == OP and t.text in ("+", "-"):
            ti = self._ti()
            op = self._advance().text
            operand = self._exp_comp()
            n = self._node(UNARY, ti, op=op, children=[operand])
            operand.parent = n
            return n
        return self._exp_comp()

    def _exp_comp(self) -> Node:
        ti = self._ti()
        node = self._comp()
        t = self._tok()
        if t.kind == OP and t.text in ("^", "**"):
            if t.text == "**":
                self.problems.append(Problem(
                    "SYN004", "'**' is not a Desmond operator; the power "
                              "operator is '^'", t.start, t.end))
            self._advance()
            rhs = self._signed()            # right-associative, may be signed
            return self._binary(ti, "^", node, rhs)
        return node

    def _comp(self) -> Node:
        ti = self._ti()
        node = self._atom()
        while self._is("["):
            self._advance()
            idx = self._expr()
            self._expect("]", "closing a subscript")
            node = self._node(INDEX, ti, children=[node, idx])
            for c in node.children:
                c.parent = node
        return node

    def _binary(self, ti: int, op: str, lhs: Node, rhs: Node) -> Node:
        n = self._node(BINARY, ti, op=op, children=[lhs, rhs])
        lhs.parent = n
        rhs.parent = n
        return n

    def _atom(self) -> Node:
        t = self._tok()
        ti = self._ti()
        if t.kind == NUMBER:
            self._advance()
            return self._node(NUM, ti, text=t.text)
        if t.kind == STRING:
            self._advance()
            return self._node(STR, ti, text=t.text)
        if t.kind == KEYWORD and t.text == "series":
            return self._series()
        if t.kind == KEYWORD and t.text == "if":
            return self._ifelse()
        if t.kind == KEYWORD and t.text in ("declare_meta", "declare_output"):
            raise _Recover(f"{t.text} is a declaration, not a value",
                           t.start, t.end)
        if t.kind == IDENT:
            self._advance()
            if self._is("("):
                self._advance()
                args: list = []
                if not self._is(")"):
                    while True:
                        args.append(self._expr())
                        if self._accept(","):
                            continue
                        break
                self._expect(")", f"closing the call to {t.text}")
                n = self._node(CALL, ti, name=t.text, children=args)
                for c in args:
                    c.parent = n
                return n
            return self._node(NAME, ti, name=t.text)
        if self._is("("):
            self._advance()
            inner = self._expr()
            self._expect(")", "closing a parenthesised expression")
            n = self._node(PAREN, ti, children=[inner])
            inner.parent = n
            return n
        if self._is("{"):
            self._advance()
            stmts = self._block_body("}")
            self._expect("}", "closing a block")
            n = self._node(BLOCK, ti, children=stmts)
            for c in stmts:
                c.parent = n
            return n
        raise _Recover(f"expected a value, found {_show(t)}", t.start, t.end)

    def _block_body(self, closer: str) -> list:
        stmts: list = []
        while not self.at_end and not self._is(closer):
            ti = self._ti()
            if self._tok().kind == KEYWORD and self._tok().text == "static":
                stmts.append(self._static())
            elif self._tok().kind == IDENT and self._is("=", 1):
                name = self._advance().text
                self._advance()
                value = self._expr()
                n = self._node(BIND, ti, name=name, children=[value])
                value.parent = n
                stmts.append(n)
            else:
                value = self._expr()
                n = self._node(EXPR_STMT, ti, children=[value])
                value.parent = n
                stmts.append(n)
            if not self._accept(";"):
                break
        return stmts

    def _series(self) -> Node:
        ti = self._ti()
        self._advance()
        self._expect("(", "after series")
        iters: list = []
        while True:
            iti = self._ti()
            name_tok = self._tok()
            if name_tok.kind != IDENT:
                raise _Recover("a series iterator needs a name",
                               name_tok.start, name_tok.end)
            name = self._advance().text
            self._expect("=", "in a series iterator")
            lo = self._expr()
            self._expect(":", "between a series iterator's bounds")
            hi = self._expr()
            it = self._node(ITER, iti, name=name, children=[lo, hi])
            for c in it.children:
                c.parent = it
            iters.append(it)
            if self._accept(","):
                continue
            break
        self._expect(")", "closing the series iterators")
        body = self._expr()
        n = self._node(SERIES, ti, children=iters + [body])
        for c in n.children:
            c.parent = n
        return n

    def _ifelse(self) -> Node:
        ti = self._ti()
        self._advance()
        cond = self._expr()
        if not self._is("then"):
            t = self._tok()
            raise _Recover(f"expected 'then', found {t.text!r}", t.start,
                           t.end)
        self._advance()
        yes = self._expr()
        if not self._is("else"):
            t = self._tok()
            raise _Recover("Desmond's 'if' is a value, so it always needs an "
                           f"'else'; found {t.text!r}", t.start, t.end)
        self._advance()
        no = self._expr()
        n = self._node(IFELSE, ti, children=[cond, yes, no])
        for c in n.children:
            c.parent = n
        return n


def _show(t: Token) -> str:
    """How a token is named in a message; the EOF token has no text."""
    return repr(t.text) if t.text else "the end of the file"


class _Recover(Exception):
    def __init__(self, message: str, start: int, end: int):
        super().__init__(message)
        self.message = message
        self.start = start
        self.end = end


def parse(src: str, *, max_depth: int = 256,
          max_tokens: int = 2_000_000) -> ParseResult:
    """Parse ``src`` into a CST, preserving everything it cannot explain.

    ``max_depth`` bounds expression nesting; the CPython recursion limit is
    raised for the duration to match, because descending one grammar level
    costs several Python frames and the interpreter's own limit would
    otherwise turn a legal - if unpleasant - file into an opaque blob well
    before ``max_depth`` was reached.  The limit is always restored.
    """
    import sys
    lex = tokenize(src, max_tokens=max_tokens)
    problems = [Problem(code, msg, a, b)
                for code, msg, a, b in lex.problems]
    for k, tok in enumerate(lex.tokens):
        if tok.kind != "comment":
            continue
        nxt = lex.tokens[k + 1] if k + 1 < len(lex.tokens) else None
        if nxt is None or nxt.kind == "eof":
            problems.append(Problem(
                "SYN006",
                "this comment is the last thing in the file and is not "
                "followed by a newline; Desmond's lexer defines a comment as "
                "running up to a newline and rejects the file without one",
                tok.start, tok.end, evidence=tok.text[:80]))
    p = _Parser(src, lex.tokens, max_depth=max_depth)
    prev = sys.getrecursionlimit()
    need = max(prev, 1000 + max_depth * 12)
    try:
        sys.setrecursionlimit(need)
        program = p.parse()
    finally:
        sys.setrecursionlimit(prev)
    problems.extend(p.problems)
    return ParseResult(src, program, lex.tokens, problems)


def coverage_gaps(res: ParseResult) -> list:
    """Non-trivia tokens that no statement claims - must always be empty.

    This is the machine-checkable form of "nothing is silently dropped": every
    token that carries meaning has to sit inside exactly one statement span.
    """
    claimed = [False] * len(res.tokens)
    for stmt in res.program.children:
        for i in range(stmt.ti, min(stmt.tj, len(res.tokens))):
            claimed[i] = True
    gaps = []
    for i, t in enumerate(res.tokens):
        if t.kind == EOF_ or t.is_trivia:
            continue
        if not claimed[i]:
            gaps.append(t)
    return gaps
