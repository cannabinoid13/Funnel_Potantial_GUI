"""Interpreter and validator for the Desmond enhanced-sampling M-expression.

Modelled on the language documented in the Desmond User's Guide, chapter 11
("Enhanced Sampling and Umbrella Sampling"):

* imperative, semicolon-separated statements; **single assignment** - a name
  may be bound only once per scope,
* every value is an *array of germs* (a number plus its differential), so a
  3-vector is simply a length-3 array and ``*`` is element-wise, not a dot
  product,
* binary threading: an operand of length 1 pairs with every element of the
  other operand,
* ``if c then a else b`` takes the positive branch when ``c > 0``,
* ``{ ... }`` blocks introduce a scope and evaluate to their last expression,
* ``series (i=lo:hi, ...) body`` sums ``body`` over the iterators,
* ``a[i]`` subscripts, ``static`` declarations with ``store``.

Only the functions Desmond actually provides are accepted (see
:data:`FUNCTIONS`).  ``abs`` and ``tan`` deliberately do not exist here
because they do not exist there either; ``min`` and ``max`` *do* exist and are
implemented, a correction to an earlier reading of the guide that was checked
against ``enhsamp.parseStr`` on the installed 2025-3 and 2020-3 suites.  The interpreter is used
for three things: validating expressions the user types, previewing their
value, and re-reading an exported ``.pot`` to check it against the model.

Everything is vectorised over an optional leading sample axis, so a whole grid
of trial positions can be pushed through one expression in a single pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .potfile import parse_asl_atoms


class MExprError(Exception):
    pass


# --------------------------------------------------------------------------
# the function table, straight from the guide's Table 11.1
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FnSpec:
    name: str
    nargs: tuple            # allowed argument counts
    cls: str                # 'normal' | 'threaded' | 'binary' | 'special'
    doc: str
    returns: str = "array"


def _f(name, nargs, cls, doc, returns="array"):
    return FnSpec(name, nargs if isinstance(nargs, tuple) else (nargs,), cls,
                  doc, returns)


FUNCTIONS: dict[str, FnSpec] = {s.name: s for s in [
    _f("acos", 1, "threaded", "arccosine, element-wise"),
    _f("angle", 2, "normal", "cosine of the angle between two vectors"),
    _f("angle_gid", 3, "normal", "cosine of the angle of three particles"),
    _f("angle_gid_radians", 3, "normal",
       "angle of three particles in radians (unstable near 0 and pi)"),
    _f("angle_radians", 2, "normal",
       "angle between two vectors in radians (unstable near 0 and pi)"),
    _f("array", (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12), "normal",
       "concatenate the arguments into one array"),
    _f("atan2", 2, "binary", "arctangent of y/x"),
    _f("atomsel", 1, "special", "particles matching an ASL string",
       returns="selection"),
    _f("center_of_geometry", 1, "normal",
       "centre of geometry of a group (atoms must be within half a cell)"),
    _f("center_of_mass", 1, "normal",
       "mass-weighted centre of a group, with periodic image handling"),
    _f("cos", 1, "threaded", "cosine, element-wise"),
    _f("cross", 2, "normal", "cross product of two 3-vectors"),
    _f("delta", 2, "normal", "min-image vector between two particles"),
    _f("dihedral", 3, "normal", "cosine and sine of a dihedral from three bond vectors"),
    _f("dihedral_radians", 3, "normal", "dihedral from three bond vectors in radians"),
    _f("dihedral_gid", 4, "normal", "cosine and sine of a four-particle dihedral"),
    _f("dihedral_gid_radians", 4, "normal", "four-particle dihedral in radians"),
    _f("dist", 2, "normal", "min-image distance between two particles"),
    _f("dot", 2, "normal", "dot product"),
    _f("elem", 2, "normal", "zero-based array subscript"),
    _f("exp", 1, "threaded", "exponential, element-wise"),
    _f("gibbs_max", 2, "normal",
       "softened maximum: T*log(sum(exp(a/T))) over the array a"),
    _f("gibbs_min", 2, "normal",
       "softened minimum: -T*log(sum(exp(-a/T))) over the array a"),
    _f("length", 1, "normal", "number of elements in an array"),
    _f("log", 1, "threaded", "natural logarithm, element-wise"),
    _f("mass", 1, "normal", "mass of a particle in amu"),
    _f("max", 1, "normal", "largest element of an array"),
    _f("min", 1, "normal", "smallest element of an array"),
    _f("meta", 3, "special",
       "metadynamics accumulator: meta(id, array(height, widths...), "
       "array(cvs...))"),
    _f("min_image", 1, "normal", "minimum image of a 3-vector"),
    _f("mod", 2, "binary", "modulus, result in [0, b)"),
    _f("ncoordination", (2, 3, 4), "normal",
       "coordination number between two groups"),
    _f("norm", 1, "normal", "length of a vector"),
    _f("norm2", 1, "normal", "squared length of a vector"),
    _f("pos", 1, "normal", "position of a particle"),
    _f("pos_inner_prod", (2, 3), "normal", "weighted sum of particle positions"),
    _f("pow", 2, "normal", "a**b for positive a (undefined for a <= 0)"),
    _f("print", 2, "special", "record a value in the CV output file"),
    _f("rad_gyration", 1, "normal", "radius of gyration of a group"),
    _f("sign", 1, "threaded", "+1 for x >= 0, -1 for x < 0"),
    _f("sin", 1, "threaded", "sine, element-wise"),
    _f("sqrt", 1, "threaded", "square root, element-wise"),
    _f("store", 2, "special", "store a value in a static variable"),
    _f("sum", 1, "normal", "sum of the elements of an array"),
    _f("time", 0, "normal", "chemical time in ps"),
    _f("declare_meta", (0,), "special", "header: metadynamics accumulator"),
    _f("declare_output", (0,), "special", "header: CV output file"),
]}

#: Functions people reach for that Desmond does not have, with the fix.
NOT_IN_DESMOND = {
    "abs": "use x*sign(x)",
    "fabs": "use x*sign(x)",
    "let": "`let` exists inside the engine but the front end rejects it; "
           "write a { ... } block instead",
    "tan": "use sin(x)/cos(x)",
    "atan": "use atan2(x, 1.0)",
    "asin": "use acos(x) with a phase shift, or atan2",
    "floor": "not available",
    "ceil": "not available",
    "round": "not available",
    "clamp": "use nested if expressions",
    "heaviside": "use if x then 1.0 else 0.0",
    "step": "use if x then 1.0 else 0.0",
}


# --------------------------------------------------------------------------
# values
# --------------------------------------------------------------------------
NUM, SEL, STR = "num", "sel", "str"


class V:
    """A value: an array of germs (last axis), optionally batched.

    ``data`` has shape ``(..., n)``; ``n`` is the Desmond array length, so a
    scalar is ``n == 1`` and a 3-vector is ``n == 3``.  Leading axes are the
    sample axes this interpreter adds for grid evaluation.
    """

    __slots__ = ("kind", "data", "name")

    def __init__(self, kind: str, data, name: str = ""):
        self.kind = kind
        self.data = data
        self.name = name

    # -- constructors
    @staticmethod
    def num(x) -> "V":
        a = np.asarray(x, dtype=np.float64)
        if a.ndim == 0:
            a = a.reshape(1)
        return V(NUM, a)

    @staticmethod
    def scalar_like(shape, value: float) -> "V":
        return V(NUM, np.full(tuple(shape) + (1,), float(value)))

    @property
    def n(self) -> int:
        return int(self.data.shape[-1]) if self.kind == NUM else \
            int(np.size(self.data))

    @property
    def batch(self) -> tuple:
        return tuple(self.data.shape[:-1]) if self.kind == NUM else ()

    def __repr__(self):
        if self.kind == NUM:
            return f"V(num,shape={self.data.shape})"
        return f"V({self.kind})"


def _num(v: V, what: str) -> np.ndarray:
    if v.kind != NUM:
        raise MExprError(f"{what} needs a numeric array, got a {v.kind}")
    return v.data


def _vec3(v: V, what: str) -> np.ndarray:
    a = _num(v, what)
    if a.shape[-1] != 3:
        raise MExprError(f"{what} needs a length-3 array, got length "
                         f"{a.shape[-1]}")
    return a


def _single(v: V, what: str) -> np.ndarray:
    a = _num(v, what)
    if a.shape[-1] != 1:
        raise MExprError(f"{what} needs a single number, got an array of "
                         f"length {a.shape[-1]}")
    return a


def _thread(a: np.ndarray, b: np.ndarray, op, what: str) -> np.ndarray:
    """Desmond binary threading: equal lengths, or one of length 1."""
    la, lb = a.shape[-1], b.shape[-1]
    if la != lb and la != 1 and lb != 1:
        raise MExprError(f"{what}: cannot combine arrays of length {la} and "
                         f"{lb} (binary threading needs equal lengths or one "
                         "of length 1)")
    try:
        return op(a, b)
    except ValueError as exc:
        raise MExprError(f"{what}: incompatible sample shapes "
                         f"{a.shape[:-1]} and {b.shape[:-1]}") from exc


def _integer(v: V, what: str) -> int:
    """A topology index or loop bound cannot vary over probe samples."""
    a = _single(v, what)
    if a.size != 1 or not np.isfinite(a).all():
        raise MExprError(f"{what} needs one finite integer")
    x = float(a.reshape(-1)[0])
    if x != int(x):
        raise MExprError(f"{what} needs an integer, got {x:g}")
    return int(x)


# --------------------------------------------------------------------------
# lexer
# --------------------------------------------------------------------------
_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>\#[^\n]*)
  | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<id>[A-Za-z_]\w*)
  | (?P<op>\*\*|[-+*/^(),;={}\[\]:])
""", re.X)

KEYWORDS = {"if", "then", "else", "series", "static"}


@dataclass
class Tok:
    kind: str
    text: str
    pos: int
    line: int


def lex(text: str) -> list[Tok]:
    out: list[Tok] = []
    i, line = (1 if text.startswith("\ufeff") else 0), 1
    n = len(text)
    while i < n:
        m = _TOKEN.match(text, i)
        if not m:
            raise MExprError(f"line {line}: unexpected character {text[i]!r}")
        kind = m.lastgroup
        s = m.group()
        i = m.end()
        if kind in ("ws", "comment"):
            line += s.count("\n")
            continue
        out.append(Tok(kind, s, m.start(), line))
        line += s.count("\n")
    out.append(Tok("eof", "", n, line))
    return out


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
class Parser:
    def __init__(self, toks: list[Tok]):
        self.t = toks
        self.i = 0

    def peek(self, k: int = 0) -> Tok:
        j = min(self.i + k, len(self.t) - 1)
        return self.t[j]

    def next(self) -> Tok:
        tok = self.t[self.i]
        if tok.kind != "eof":
            self.i += 1
        return tok

    def expect(self, text: str) -> Tok:
        tok = self.next()
        if tok.text != text:
            raise MExprError(f"line {tok.line}: expected {text!r}, found "
                             f"{tok.text or 'end of input'!r}")
        return tok

    # -- program / statements
    def program(self) -> list:
        stmts = []
        while self.peek().kind != "eof":
            stmts.append(self.statement())
        return stmts

    def statement(self, block_final: bool = False):
        tok = self.peek()
        if tok.kind == "id" and tok.text == "static":
            self.next()
            names = []
            while True:
                nm = self.next()
                if nm.kind != "id":
                    raise MExprError(f"line {nm.line}: expected a static "
                                     "variable name")
                size = 1
                if self.peek().text == "(":
                    self.next()
                    size = int(float(self.next().text))
                    self.expect(")")
                names.append((nm.text, size))
                if self.peek().text == ",":
                    self.next()
                    continue
                break
            self.expect(";")
            return ("static", names, tok.line)
        if tok.kind == "id" and self.peek(1).text == "=" and \
                tok.text not in KEYWORDS:
            name = self.next().text
            self.expect("=")
            e = self.expr()
            self.expect(";")
            return ("assign", name, e, tok.line)
        e = self.expr()
        if not (block_final and self.peek().text == "}"):
            self.expect(";")
        return ("expr", e, tok.line)

    # -- expressions
    def expr(self):
        tok = self.peek()
        if tok.kind == "id" and tok.text == "if":
            self.next()
            cond = self.expr()
            self.expect("then")
            a = self.expr()
            self.expect("else")
            b = self.expr()
            return ("ifelse", cond, a, b, tok.line)
        if tok.kind == "id" and tok.text == "series":
            self.next()
            self.expect("(")
            iters = []
            while True:
                nm = self.next()
                if nm.kind != "id":
                    raise MExprError(f"line {nm.line}: expected an iterator "
                                     "name")
                self.expect("=")
                lo = self.expr()
                self.expect(":")
                hi = self.expr()
                iters.append((nm.text, lo, hi))
                if self.peek().text == ",":
                    self.next()
                    continue
                break
            self.expect(")")
            body = self.expr()
            return ("series", iters, body, tok.line)
        return self.add()

    def add(self):
        node = self.mul()
        while self.peek().text in ("+", "-"):
            op = self.next().text
            node = ("bin", op, node, self.mul())
        return node

    def mul(self):
        node = self.factor()
        while self.peek().text in ("*", "/"):
            op = self.next().text
            node = ("bin", op, node, self.factor())
        return node

    def factor(self):
        """Unary sign binds *looser* than ``^``, as in C and Python.

        So ``-x^2`` is ``-(x^2)``.  The guide says the operators obey the
        normal precedence rules; the checker additionally flags this pattern
        as worth bracketing so no reader has to know that.
        """
        tok = self.peek()
        if tok.text in ("-", "+"):
            self.next()
            return ("unary", tok.text, self.factor(), tok.line)
        return self.power()

    def power(self):
        node = self.postfix()
        if self.peek().text in ("^", "**"):
            line = self.next().line
            return ("pow", node, self.factor(), line)
        return node

    def unary(self):
        return self.factor()

    def postfix(self):
        node = self.atom()
        while self.peek().text == "[":
            line = self.next().line
            idx = self.expr()
            self.expect("]")
            node = ("index", node, idx, line)
        return node

    def atom(self):
        tok = self.next()
        if tok.kind == "num":
            return ("num", float(tok.text))
        if tok.kind == "str":
            return ("str", _unquote(tok.text))
        if tok.text == "(":
            e = self.expr()
            self.expect(")")
            return e
        if tok.text == "{":
            stmts = []
            while self.peek().text != "}":
                if self.peek().kind == "eof":
                    raise MExprError(f"line {tok.line}: unclosed block")
                stmts.append(self.statement(block_final=True))
            self.expect("}")
            if not stmts or stmts[-1][0] != "expr":
                raise MExprError(f"line {tok.line}: the last statement of a "
                                 "block must be an expression, not an "
                                 "assignment")
            return ("block", stmts, tok.line)
        if tok.kind == "id":
            if tok.text in KEYWORDS:
                raise MExprError(f"line {tok.line}: unexpected {tok.text!r}")
            if self.peek().text == "(":
                self.next()
                args, kwargs = [], {}
                if self.peek().text != ")":
                    while True:
                        if (self.peek().kind == "id"
                                and self.peek(1).text == "="):
                            key = self.next().text
                            self.next()
                            kwargs[key] = self.expr()
                        else:
                            args.append(self.expr())
                        if self.peek().text == ",":
                            self.next()
                            continue
                        break
                self.expect(")")
                return ("call", tok.text, args, kwargs, tok.line)
            return ("var", tok.text, tok.line)
        raise MExprError(f"line {tok.line}: unexpected "
                         f"{tok.text or 'end of input'!r}")


def _unquote(s: str) -> str:
    return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")


def parse(text: str) -> list:
    return Parser(lex(text)).program()


def parse_expression(text: str):
    """Parse a single expression (no trailing semicolon required)."""
    toks = lex(text)
    p = Parser(toks)
    node = p.expr()
    if p.peek().text == ";":
        p.next()
    if p.peek().kind != "eof":
        raise MExprError(f"line {p.peek().line}: unexpected trailing "
                         f"{p.peek().text!r}")
    return node


# --------------------------------------------------------------------------
# static checks
# --------------------------------------------------------------------------
def check_tree(node, known: set[str], where: str = "expression") -> list[str]:
    """Report Desmond-level problems without evaluating anything."""
    problems: list[str] = []

    def walk(n, scope: set[str]):
        tag = n[0]
        if tag in ("num", "str"):
            return
        if tag == "var":
            if n[1] not in scope:
                problems.append(f"unknown variable `{n[1]}` (line {n[2]})")
            return
        if tag == "unary":
            if n[1] == "-" and n[2][0] == "pow":
                line = n[3] if len(n) > 3 else 0
                problems.append(
                    f"`-a^b` is read as -(a^b) here; write it with brackets so "
                    f"no parser can disagree (line {line})")
            walk(n[2], scope)
            return
        if tag == "bin":
            walk(n[2], scope)
            walk(n[3], scope)
            return
        if tag == "pow":
            walk(n[1], scope)
            walk(n[2], scope)
            exp = n[2]
            if exp[0] == "num":
                if abs(exp[1] - round(exp[1])) > 1e-12:
                    problems.append(
                        f"`^` raises to an integer power only; use "
                        f"pow(base, {exp[1]:g}) instead (line {n[3]})")
            elif exp[0] == "unary" and exp[2][0] == "num":
                if abs(exp[2][1] - round(exp[2][1])) > 1e-12:
                    problems.append(
                        "`^` raises to an integer power only; use pow() for "
                        f"fractional exponents (line {n[3]})")
            else:
                problems.append(
                    "the exponent of `^` must be an integer literal in "
                    f"Desmond; use pow(base, exponent) (line {n[3]})")
            return
        if tag == "index":
            walk(n[1], scope)
            walk(n[2], scope)
            return
        if tag == "ifelse":
            for k in (1, 2, 3):
                walk(n[k], scope)
            return
        if tag == "series":
            inner = set(scope)
            for name, lo, hi in n[1]:
                walk(lo, scope)
                walk(hi, scope)
                inner.add(name)
            walk(n[2], inner)
            return
        if tag == "block":
            inner = set(scope)
            for st in n[1]:
                if st[0] == "assign":
                    walk(st[2], inner)
                    inner.add(st[1])
                elif st[0] == "expr":
                    walk(st[1], inner)
            return
        if tag == "call":
            name, args, kwargs = n[1], n[2], n[3]
            line = n[4]
            if name in NOT_IN_DESMOND:
                problems.append(
                    f"`{name}()` does not exist in Desmond - "
                    f"{NOT_IN_DESMOND[name]} (line {line})")
            elif name not in FUNCTIONS:
                problems.append(f"unknown function `{name}()` (line {line})")
            else:
                spec = FUNCTIONS[name]
                if name not in ("declare_meta", "declare_output", "array") and \
                        len(args) not in spec.nargs:
                    problems.append(
                        f"`{name}()` takes {' or '.join(str(x) for x in spec.nargs)}"
                        f" argument(s), got {len(args)} (line {line})")
            for a in args:
                walk(a, scope)
            for a in kwargs.values():
                walk(a, scope)
            return
        raise MExprError(f"cannot check node {tag}")

    walk(node, set(known))
    return problems


def functions_reference() -> list[tuple[str, str]]:
    """(signature, description) for every function, for the UI palette."""
    out = []
    for name in sorted(FUNCTIONS):
        spec = FUNCTIONS[name]
        if name in ("declare_meta", "declare_output"):
            continue
        n = spec.nargs[0]
        args = ", ".join("a b c d e f".split()[:n]) if n else ""
        out.append((f"{name}({args})", spec.doc))
    return out


# --------------------------------------------------------------------------
# interpreter
# --------------------------------------------------------------------------
@dataclass
class Result:
    variables: dict = field(default_factory=dict)
    prints: list = field(default_factory=list)
    declares: dict = field(default_factory=dict)
    value: V | None = None
    meta_calls: int = 0
    warnings: list = field(default_factory=list)

    def scalar(self, name: str, default=float("nan")) -> float:
        v = self.variables.get(name)
        if v is None or v.kind != NUM:
            return default
        a = np.asarray(v.data)
        return float(a.reshape(-1)[0]) if a.size else default

    def array(self, name: str):
        v = self.variables.get(name)
        if v is None or v.kind != NUM:
            return None
        a = np.asarray(v.data)
        return a[..., 0] if a.shape[-1] == 1 else a

    def printed(self) -> dict:
        out = {}
        for label, val in self.prints:
            a = np.asarray(val)
            out[label] = float(a.reshape(-1)[0]) if a.size else float("nan")
        return out


class Interpreter:
    """Evaluate an M-expression against a :class:`Structure`."""

    def __init__(self, structure, com_override: dict | None = None,
                 meta_bias: float = 0.0, time_ps: float = 0.0,
                 env: dict | None = None):
        self.st = structure
        self.com_override = com_override or {}
        self.meta_bias = meta_bias
        self.time_ps = time_ps
        self.extra_env = env or {}
        #: true centre of each overridden group, filled in when it is bound.
        #: A probe moves a group's centre of mass; a term that reads the
        #: atoms themselves (``pos(g[i])``) has to move with it, rigidly,
        #: or the file and the model would be evaluating different physics.
        self._group_com0: dict = {}
        self._atom_overrides: dict[int, str] = {}

    # ------------------------------------------------------------------
    def run(self, text: str) -> Result:
        return self.run_tree(parse(text))

    def run_tree(self, stmts) -> Result:
        res = Result()
        self._group_com0.clear()
        self._atom_overrides.clear()
        env: dict[str, V] = dict(self.extra_env)
        statics: dict[str, V] = {}
        for st in stmts:
            if st[0] == "static":
                for name, size in st[1]:
                    statics[name] = V.num(np.zeros(size))
                    env[name] = statics[name]
                res.warnings.append(
                    "static variables are previewed as zero; their real value "
                    "only exists during the run")
                continue
            if st[0] == "assign":
                _, name, e, line = st
                if name in env:
                    raise MExprError(
                        f"line {line}: `{name}` is already bound - the "
                        "M-expression language allows a single assignment per "
                        "name")
                try:
                    env[name] = self._eval(e, env, res)
                except MExprError as exc:
                    raise MExprError(f"line {line}: while assigning "
                                     f"`{name}`: {exc}") from None
                if env[name].kind == SEL:
                    # Selection aliases must not mutate the original value.
                    env[name] = V(SEL, env[name].data, name)
                    if name in self.com_override and self.st is not None:
                        self._group_com0[name] = self.st.center_of_mass(
                            np.asarray(env[name].data, dtype=np.int64))
                        for gid in np.asarray(env[name].data).reshape(-1):
                            old = self._atom_overrides.get(int(gid))
                            if old is not None and old != name:
                                raise MExprError(
                                    f"probe groups `{old}` and `{name}` overlap")
                            self._atom_overrides[int(gid)] = name
            else:
                _, e, line = st
                try:
                    v = self._eval(e, env, res)
                except MExprError as exc:
                    raise MExprError(f"line {line}: {exc}") from None
                if v is not None and v.kind == NUM:
                    res.value = v
        res.variables = env
        return res

    def eval_expression(self, text: str, env: dict,
                        res: Result | None = None) -> V:
        """Evaluate one expression in a prepared environment."""
        res = res or Result()
        return self._eval(parse_expression(text), env, res)

    # ------------------------------------------------------------------
    def _eval(self, node, env, res) -> V:
        tag = node[0]
        if tag == "num":
            return V.num(node[1])
        if tag == "str":
            return V(STR, node[1])
        if tag == "var":
            name = node[1]
            if name in env:
                return env[name]
            raise MExprError(f"undefined variable `{name}`")
        if tag == "unary":
            v = self._eval(node[2], env, res)
            if node[1] == "+":
                return v
            return V(NUM, -_num(v, "unary minus"))
        if tag == "bin":
            op = node[1]
            a = _num(self._eval(node[2], env, res), f"operator {op}")
            b = _num(self._eval(node[3], env, res), f"operator {op}")
            if op == "+":
                return V(NUM, _thread(a, b, lambda x, y: x + y, "+"))
            if op == "-":
                return V(NUM, _thread(a, b, lambda x, y: x - y, "-"))
            if op == "*":
                return V(NUM, _thread(a, b, lambda x, y: x * y, "*"))
            if op == "/":
                with np.errstate(divide="ignore", invalid="ignore"):
                    return V(NUM, _thread(a, b, lambda x, y: x / y, "/"))
            raise MExprError(f"unknown operator {op}")
        if tag == "pow":
            a = _num(self._eval(node[1], env, res), "^")
            b = _num(self._eval(node[2], env, res), "^")
            with np.errstate(all="ignore"):
                return V(NUM, _thread(a, b, np.power, "^"))
        if tag == "index":
            base = self._eval(node[1], env, res)
            idx = self._eval(node[2], env, res)
            k = _integer(idx, "subscript")
            if base.kind == SEL:
                arr = np.asarray(base.data)
                if not (0 <= k < arr.size):
                    raise MExprError(f"selection subscript {k} out of range "
                                     f"0..{arr.size - 1}")
                return V(SEL, arr[k:k + 1], base.name)
            a = _num(base, "subscript")
            if not (0 <= k < a.shape[-1]):
                raise MExprError(f"subscript {k} out of range 0.."
                                 f"{a.shape[-1] - 1}")
            return V(NUM, a[..., k:k + 1])
        if tag == "ifelse":
            cond = _single(self._eval(node[1], env, res), "if condition")
            a = self._eval(node[2], env, res)
            b = self._eval(node[3], env, res)
            if a.kind != NUM or b.kind != NUM:
                raise MExprError("if branches must be numeric")
            da, db = a.data, b.data
            if da.shape[-1] != db.shape[-1] and 1 not in (
                    da.shape[-1], db.shape[-1]):
                raise MExprError("if branches must have the same array length")
            try:
                return V(NUM, np.where(cond > 0.0, da, db))
            except ValueError as exc:
                raise MExprError("if branches have incompatible sample shapes") from exc
        if tag == "block":
            inner = dict(env)
            out = None
            for st in node[1]:
                if st[0] == "assign":
                    _, name, e, line = st
                    inner[name] = self._eval(e, inner, res)
                elif st[0] == "expr":
                    out = self._eval(st[1], inner, res)
                elif st[0] == "static":
                    raise MExprError("static declarations belong in the header")
            if out is None:
                raise MExprError("a block must end with an expression")
            return out
        if tag == "series":
            iters, body = node[1], node[2]
            ranges = []
            for name, lo, hi in iters:
                lo_v = _integer(self._eval(lo, env, res), "series bound")
                hi_v = _integer(self._eval(hi, env, res), "series bound")
                ranges.append((name, lo_v, hi_v))
            total = None
            counts = [max(0, hi - lo) for _, lo, hi in ranges]
            if any(c == 0 for c in counts):
                return V.num(0.0)
            if int(np.prod(counts)) > 200000:
                raise MExprError("series would iterate more than 200000 times")

            def rec(k: int, scope: dict):
                nonlocal total
                if k == len(ranges):
                    val = _num(self._eval(body, scope, res), "series body")
                    total = val if total is None else _thread(
                        total, val, lambda x, y: x + y, "series")
                    return
                name, lo, hi = ranges[k]
                for i in range(lo, hi):
                    scope[name] = V.num(float(i))
                    rec(k + 1, scope)
            rec(0, dict(env))
            return V(NUM, total)
        if tag == "call":
            return self._call(node, env, res)
        raise MExprError(f"cannot evaluate node {tag}")

    # ------------------------------------------------------------------
    def _atom_xyz(self, sel: V, gid: int):
        """One atom's position, moved with its group when a probe is set."""
        p = self.st.xyz[gid]
        key = self._atom_overrides.get(gid, "")
        if key in self.com_override and key in self._group_com0:
            shift = (np.asarray(self.com_override[key], dtype=np.float64)
                     - self._group_com0[key])
            return p + shift
        return p

    def _group_xyz(self, sel: V, what: str) -> np.ndarray:
        gids = self._gids(sel, what)
        if self.st is None:
            raise MExprError(f"{what}() needs a structure")
        if not gids.size:
            raise MExprError(f"{what}() needs a nonempty selection")
        if not any(int(gid) in self._atom_overrides for gid in gids):
            return self.st.xyz[gids]
        pts = [self._atom_xyz(sel, int(gid)) for gid in gids]
        return np.stack(np.broadcast_arrays(*pts), axis=-2)

    def _particle_xyz(self, sel: V, what: str) -> np.ndarray:
        gids = self._gids(sel, what)
        if self.st is None:
            raise MExprError(f"{what}() needs a structure")
        if gids.size != 1:
            raise MExprError(f"{what}() needs one particle, got {gids.size}")
        return self._atom_xyz(sel, int(gids[0]))

    def _gids(self, v: V, what: str) -> np.ndarray:
        if v.kind != SEL:
            raise MExprError(f"{what} needs an atom selection")
        return np.asarray(v.data, dtype=np.int64)

    def _batch_shape(self, *values) -> tuple:
        for v in values:
            if isinstance(v, V) and v.kind == NUM and v.batch:
                return v.batch
        return ()

    def _call(self, node, env, res) -> V:
        _, name, args, kwargs, line = node
        st = self.st

        if name in NOT_IN_DESMOND:
            raise MExprError(f"`{name}()` does not exist in the Desmond "
                             f"M-expression language - {NOT_IN_DESMOND[name]}")
        if name not in FUNCTIONS:
            raise MExprError(f"unknown function `{name}()`")
        spec = FUNCTIONS[name]
        if name not in ("declare_meta", "declare_output", "array") and \
                len(args) not in spec.nargs:
            raise MExprError(f"`{name}()` takes "
                             f"{' or '.join(str(x) for x in spec.nargs)} "
                             f"argument(s), got {len(args)}")

        # -- header statements
        if name in ("declare_meta", "declare_output"):
            out = {}
            for k, e in kwargs.items():
                v = self._eval(e, env, res)
                out[k] = v.data if v.kind == STR else float(
                    np.asarray(_num(v, name)).reshape(-1)[0])
            res.declares[name] = out
            return V.num(0.0)

        # -- special forms
        if name == "print":
            label = self._eval(args[0], env, res)
            val = self._eval(args[1], env, res)
            res.prints.append((label.data if label.kind == STR
                               else str(label.data),
                               np.asarray(_num(val, "print"))))
            return val
        if name == "atomsel":
            s = self._eval(args[0], env, res)
            if s.kind != STR:
                raise MExprError("atomsel() needs a string")
            try:
                if st is None:
                    idx = parse_asl_atoms(s.data)
                else:
                    from .document import _resolve_asl
                    from . import asl
                    idx = _resolve_asl(s.data, st, asl)
            except Exception as exc:
                raise MExprError(f"atomsel({s.data!r}): {exc}") from exc
            if not idx:
                raise MExprError(f"atomsel({s.data!r}) selects no atoms")
            arr = np.asarray(idx, dtype=np.int64) - 1
            if st is not None:
                bad = arr[(arr < 0) | (arr >= st.n_atoms)]
                if bad.size:
                    raise MExprError(f"atomsel() index {int(bad[0]) + 1} is "
                                     f"outside 1..{st.n_atoms}")
            return V(SEL, arr)
        if name == "store":
            self._eval(args[1], env, res)
            return V.num(0.0)
        if name == "meta":
            res.meta_calls += 1
            cvs = self._eval(args[2], env, res)
            shape = cvs.batch if cvs.kind == NUM else ()
            return V(NUM, np.full(tuple(shape) + (1,), float(self.meta_bias)))

        # -- normal / threaded functions
        if name == "time":
            return V.num(self.time_ps)
        if name == "array":
            vals = [self._eval(a, env, res) for a in args]
            arrs = [_num(v, "array") for v in vals]
            if not arrs:
                raise MExprError("array() needs at least one argument")
            try:
                batch = np.broadcast_shapes(*(a.shape[:-1] for a in arrs))
                arrs = [np.broadcast_to(a, batch + (a.shape[-1],)) for a in arrs]
            except ValueError as exc:
                raise MExprError("array() has incompatible sample shapes") from exc
            return V(NUM, np.concatenate(arrs, axis=-1))
        if name == "elem":
            return self._eval(("index", args[0], args[1], line), env, res)
        if name == "length":
            v = self._eval(args[0], env, res)
            n = v.n
            return V.num(float(n))
        if name == "sum":
            a = _num(self._eval(args[0], env, res), "sum")
            return V(NUM, a.sum(axis=-1, keepdims=True))
        if name in ("min", "max"):
            a = _num(self._eval(args[0], env, res), name)
            fn = np.min if name == "min" else np.max
            return V(NUM, fn(a, axis=-1, keepdims=True))
        if name in ("sqrt", "exp", "log", "sin", "cos", "acos", "sign"):
            a = _num(self._eval(args[0], env, res), name)
            fn = {"sqrt": np.sqrt, "exp": np.exp, "log": np.log,
                  "sin": np.sin, "cos": np.cos, "acos": np.arccos,
                  "sign": lambda x: np.where(x >= 0.0, 1.0, -1.0)}[name]
            with np.errstate(all="ignore"):
                return V(NUM, fn(a))
        if name in ("mod", "atan2", "pow"):
            a = _num(self._eval(args[0], env, res), name)
            b = _num(self._eval(args[1], env, res), name)
            with np.errstate(all="ignore"):
                if name == "mod":
                    return V(NUM, _thread(a, b, np.mod, name))
                if name == "atan2":
                    return V(NUM, _thread(a, b, np.arctan2, name))
                if np.any(a <= 0):
                    res.warnings.append(
                        "pow() was given a non-positive base; Desmond leaves "
                        "that undefined")
                return V(NUM, _thread(a, b, np.power, name))
        if name in ("gibbs_min", "gibbs_max"):
            T = _single(self._eval(args[0], env, res), name)
            a = _num(self._eval(args[1], env, res), name)
            if np.any(T <= 0) or not np.isfinite(T).all():
                raise MExprError(f"{name}() needs a positive scaling "
                                 "temperature")
            # log-sum-exp, shifted so the preview does not overflow
            with np.errstate(all="ignore"):
                if name == "gibbs_max":
                    m = a.max(axis=-1, keepdims=True)
                    out = m + T * np.log(np.exp((a - m) / T).sum(
                        axis=-1, keepdims=True))
                else:
                    m = a.min(axis=-1, keepdims=True)
                    out = m - T * np.log(np.exp(-(a - m) / T).sum(
                        axis=-1, keepdims=True))
            return V(NUM, out)
        if name == "min_image":
            a = _vec3(self._eval(args[0], env, res), "min_image")
            if st is None:
                return V(NUM, a)
            return V(NUM, st.min_image(a))
        if name == "norm":
            a = _num(self._eval(args[0], env, res), "norm")
            return V(NUM, np.sqrt((a * a).sum(axis=-1, keepdims=True)))
        if name == "norm2":
            a = _num(self._eval(args[0], env, res), "norm2")
            return V(NUM, (a * a).sum(axis=-1, keepdims=True))
        if name == "dot":
            a = _num(self._eval(args[0], env, res), "dot")
            b = _num(self._eval(args[1], env, res), "dot")
            return V(NUM, _thread(a, b, lambda x, y: x * y, "dot").sum(
                axis=-1, keepdims=True))
        if name == "cross":
            a = _vec3(self._eval(args[0], env, res), "cross")
            b = _vec3(self._eval(args[1], env, res), "cross")
            return V(NUM, np.cross(np.broadcast_to(a, np.broadcast_shapes(
                a.shape, b.shape)), np.broadcast_to(b, np.broadcast_shapes(
                    a.shape, b.shape))))
        if name in ("angle", "angle_radians"):
            a = _vec3(self._eval(args[0], env, res), name)
            b = _vec3(self._eval(args[1], env, res), name)
            na = np.sqrt((a * a).sum(axis=-1, keepdims=True))
            nb = np.sqrt((b * b).sum(axis=-1, keepdims=True))
            c = (a * b).sum(axis=-1, keepdims=True) / (na * nb)
            return V(NUM, c if name == "angle" else np.arccos(
                np.clip(c, -1.0, 1.0)))
        if name in ("angle_gid", "angle_gid_radians"):
            p = [self._particle_xyz(self._eval(arg, env, res), name)
                 for arg in args]
            a, b = st.min_image(p[0] - p[1]), st.min_image(p[2] - p[1])
            with np.errstate(all="ignore"):
                c = np.sum(a * b, axis=-1, keepdims=True) / (
                    np.linalg.norm(a, axis=-1, keepdims=True)
                    * np.linalg.norm(b, axis=-1, keepdims=True))
            return V(NUM, c if name == "angle_gid" else np.arccos(
                np.clip(c, -1.0, 1.0)))
        if name in ("dihedral", "dihedral_radians", "dihedral_gid",
                    "dihedral_gid_radians"):
            if "_gid" in name:
                pts = [self._particle_xyz(self._eval(arg, env, res), name)
                       for arg in args]
                bonds = [st.min_image(pts[i + 1] - pts[i]) for i in range(3)]
            else:
                bonds = [_vec3(self._eval(arg, env, res), name) for arg in args]
            a, b, c = np.broadcast_arrays(*bonds)
            n0, n1 = np.cross(a, b), np.cross(b, c)
            with np.errstate(all="ignore"):
                denom = (np.linalg.norm(n0, axis=-1, keepdims=True)
                         * np.linalg.norm(n1, axis=-1, keepdims=True))
                cosine = (n0 * n1).sum(axis=-1, keepdims=True) / denom
                sine = (np.cross(n0, n1) * b).sum(axis=-1, keepdims=True) / (
                    denom * np.linalg.norm(b, axis=-1, keepdims=True))
            if name.endswith("_radians"):
                return V(NUM, np.arctan2(sine, cosine))
            return V(NUM, np.concatenate((cosine, sine), axis=-1))
        if name in ("center_of_mass", "center_of_geometry", "rad_gyration"):
            sel = self._eval(args[0], env, res)
            gids = self._gids(sel, name)
            # Without topology a full group's supplied COM can still be used
            # for expression editing. With topology derive every subset and
            # alias from the same rigid displacement of its actual atoms.
            if st is None and name == "center_of_mass" and sel.name in self.com_override:
                return V(NUM, np.asarray(self.com_override[sel.name],
                                         dtype=np.float64))
            pts = self._group_xyz(sel, name)
            if name == "center_of_geometry":
                return V(NUM, pts.mean(axis=-2))
            m = st.mass[gids]
            c = ((pts * m[:, None]).sum(axis=-2) / m.sum()
                 if m.sum() > 0 else pts.mean(axis=-2))
            if name == "center_of_mass":
                return V(NUM, c)
            rg = np.sqrt((m * ((pts - c[..., None, :]) ** 2).sum(axis=-1)).sum(
                axis=-1, keepdims=True) / max(m.sum(), 1e-12))
            return V(NUM, rg)
        if name == "pos":
            sel = self._eval(args[0], env, res)
            gids = self._gids(sel, "pos")
            if st is None:
                raise MExprError("pos() needs a structure")
            if gids.size != 1:
                raise MExprError(
                    f"pos() takes one particle, not a group of {gids.size} - "
                    "Desmond rejects this too; subscript the selection, as in "
                    "series (i=0:length(g)) pos(g[i])")
            return V(NUM, self._atom_xyz(sel, int(gids.reshape(-1)[0])))
        if name == "mass":
            gids = self._gids(self._eval(args[0], env, res), "mass")
            if st is None:
                raise MExprError("mass() needs a structure")
            return V(NUM, np.asarray(st.mass[gids], dtype=float))
        if name in ("delta", "dist"):
            s2 = self._eval(args[0], env, res)
            s1 = self._eval(args[1], env, res)
            p2 = self._particle_xyz(s2, name)
            p1 = self._particle_xyz(s1, name)
            v = st.min_image(p2 - p1)
            if name == "delta":
                return V(NUM, v)
            return V(NUM, np.linalg.norm(v, axis=-1, keepdims=True))
        if name == "pos_inner_prod":
            sel = self._eval(args[0], env, res)
            gids = self._gids(sel, name)
            w = _num(self._eval(args[1], env, res), name)
            if st is None:
                raise MExprError("pos_inner_prod() needs a structure")
            if w.shape[-1] != gids.size:
                raise MExprError("pos_inner_prod() needs one weight per atom")
            return V(NUM, (self._group_xyz(sel, name) * w[..., None]).sum(axis=-2))
        if name == "ncoordination":
            raise MExprError("ncoordination() is recognised but not evaluated "
                             "by this preview; it will still work in Desmond")
        if name in ("contact_map", "helix", "rmsd", "rmsd_torsion",
                    "dihedral", "dihedral_gid", "dihedral_radians",
                    "dihedral_gid_radians", "angle_gid",
                    "angle_gid_radians"):
            raise MExprError(f"{name}() is a valid Desmond function but this "
                             "preview cannot evaluate it")
        raise MExprError(f"line {line}: `{name}()` is not implemented in the "
                         "preview interpreter")


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------
def evaluate_pot(text: str, structure, probe=None, probe_group: str = "lig",
                 meta_bias: float = 0.0, time_ps: float = 0.0) -> Result:
    """Evaluate a potential file, optionally with one group's COM replaced.

    ``probe`` may be a single position or an (M, 3) array of trial centres;
    every value in the result then carries the same leading shape.
    """
    over = {}
    if probe is not None:
        p = np.asarray(probe, dtype=np.float64)
        over[probe_group] = p
    return Interpreter(structure, over, meta_bias=meta_bias,
                       time_ps=time_ps).run(text)


def check_expression(text: str, known: set[str]) -> list[str]:
    """Parse and statically check one expression; [] means it is clean."""
    try:
        tree = parse_expression(text)
    except MExprError as exc:
        return [str(exc)]
    return check_tree(tree, known)
