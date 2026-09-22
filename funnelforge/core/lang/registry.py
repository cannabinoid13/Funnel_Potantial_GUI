"""Declarative, version-aware registry of the Desmond M-expression language.

What this module is
-------------------
A *description* of which names a given Desmond release accepts and what shapes
it accepts them in.  It performs no parsing and no checking; it is the table
that :mod:`funnelforge.core.lang.sema` and the GUI consult so that the editor
and the compiler agree on what the language contains.

The table is derived from the installation on this machine, not from the
manual.  The authority is ``getFcnSigs()`` in::

    $SCHRODINGER/internal/lib/python3.11/site-packages/schrodinger/
        application/desmond/enhanced_sampling/FcnTypes.py

That function is what ``enhsamp.Env`` loads, and every function call in a
``.pot`` is type-checked against it before Desmond will run.  Two installed
suites were read to build this file, Schrodinger 2025-3 Build 160 and
Schrodinger 2020-3 Build 139; their tables and their grammars are byte-for-byte
identical, so the language has not moved in five releases.

The type system: "dimensionality"
---------------------------------
Every value in the language is a fixed-length array of numbers, and the whole
type of a value is that length.  The signature convention is documented in a
comment above ``SimpleSyntax`` in the stale Python-2 sibling copy of the same
file (``.../desmond/packages/enhanced_sampling/FcnTypes.py``, lines 33-41),
which spells out the four cases:

* a non-negative integer is a fixed array length, so ``3`` means "a 3-vector"
  and ``1`` means "a scalar";
* a negative integer is a wildcard bound by unification across the signature -
  in that comment's words, "all -1's are the same dimension all -2's the same";
  a *return* type may be a wildcard as long as some argument pins it down;
* ``'string'`` is the string type, which only ``print`` and the special forms
  accept;
* there is no scalar/vector distinction beyond the length, and no element type
  at all - everything is a double.

:class:`Signature` records exactly those codes in :attr:`Signature.ret` and
:attr:`Signature.args`, unchanged, so a consumer can reimplement the same
unification without re-deriving the convention.

Four kinds of signature
-----------------------
``simple``
    ``ret`` and ``args`` are the literal table entries; arity is fixed at
    ``len(args)``.
``thread``
    a one-argument element-wise function.  ``args`` is ``(-1,)`` and ``ret`` is
    ``-1``: the result has exactly the argument's length.
``binary_thread``
    the arithmetic operators plus ``atan2`` and ``mod``.  ``args`` is
    ``(-1, -1)`` and ``ret`` is ``-1``, but the unification rule is looser than
    for ``simple``: if either operand has length 1 it pairs with every element
    of the other and the result takes the other's length; otherwise the two
    lengths must match.  This is why ``2 * pos(i)`` is legal.
``special``
    a form whose typing rule is not expressible as a signature - ``array``
    (result length is the sum of the argument lengths), ``meta`` (argument 2
    must be one longer than argument 3), ``rmsd``, and the four forms that
    ``enhsamp.py`` intercepts before the table is ever consulted.  ``args`` is
    ``()`` for every one of these; ``nargs`` and ``doc`` carry what is known.

Names the table does not contain
--------------------------------
Two directions of mismatch matter, and conflating them misleads a user:

*Accepted but absent from the table.*  ``enhsamp.py`` special-cases four names
before type checking: ``atomsel`` is rewritten into a literal ``array`` of gids
by ``resolve_atomsel``; ``load`` and ``store`` are typed against the ``static``
declarations in ``FcnCall.get_type``; and a bare reference to a ``static`` name
is silently rewritten into ``load("name")``.  They are recorded here with
``kind='special'`` because a validator that only consulted ``getFcnSigs()``
would wrongly reject them.  ``declare_meta`` and ``declare_output`` are header
declarations in the grammar rather than calls, and are recorded for the same
reason.

*Present in the engine but rejected by the front end.*  These are the dangerous
ones: the interface must never tell a user a name is fine when ``enhsamp`` will
refuse to compile it.  :func:`backend_only` returns them per release.  The
verified instance is ``let``: the compiler emits ``[let [...] ...]`` as the head
of every block it lowers, so the backend evaluator plainly implements it, yet
``let(...)`` written in a ``.pot`` raises ``KeyError: 'let'`` because there is no
signature for it.  ``pow`` is the historical instance - it is in both installed
tables but not in the older one preserved under ``packages/``.

Note on ``min`` and ``max``: they are frequently described as backend-only, and
:mod:`funnelforge.core.mexpr` still says so.  That is wrong for every release
recorded here.  ``SimpleSyntax('min', 1, [-1])`` and its ``max`` counterpart are
in the 2020-3 and 2025-3 tables, and ``min(array(1.0, 2.0, 3.0))`` type-checks
against the real compiler.  Verified rather than assumed, because the cost of
the assumption is telling a user to rewrite working input.

Unknown is not invalid
----------------------
:func:`known` returning ``False`` means *this registry does not recognise the
name*, never *the name is wrong*.  A ``.pot`` may come from a newer suite, or
from a site build with extra collective variables.  Callers must render an
unrecognised name as "unrecognised, preserved" and must not block an export on
it.  The ``generic`` release exists for the same reason: when the installed
Desmond version cannot be determined, it supplies the union of everything known
with ``validated=False`` so that nothing is rejected for lack of a version.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_RELEASE",
    "GENERIC_RELEASE",
    "Signature",
    "Release",
    "RELEASES",
    "EXPRESSION_KEYWORDS",
    "DECLARATION_KEYWORDS",
    "DECLARATION_ATTRIBUTES",
    "RESERVED_WORDS",
    "signatures",
    "get",
    "known",
    "is_side_effecting",
    "backend_only",
    "capability_matrix",
    "release_for_installation",
    "installation_version",
    "format_dim",
    "format_signature",
]

#: the release assumed when a caller does not say which one it means.
DEFAULT_RELEASE = "2025-3"

#: the version-agnostic fallback, used when the installation is unknown.
GENERIC_RELEASE = "generic"


# --------------------------------------------------------------------------
# the reserved words, straight from mexp.g
# --------------------------------------------------------------------------
# The grammar declares these as implicit literal tokens, which in ANTLR3 take
# priority over IDENT.  That has a consequence people trip over: the six
# declaration attribute words are reserved *everywhere*, not just inside a
# declaration, so `name = 1.0;` is a syntax error and not a binding.  Verified
# against the real parser for every word below.

#: reserved words that appear inside expressions.
EXPRESSION_KEYWORDS = frozenset({"if", "then", "else", "series", "static"})

#: reserved words that introduce a header declaration.
DECLARATION_KEYWORDS = frozenset({"declare_meta", "declare_output"})

#: reserved words usable only as declaration attribute names, plus the ``inf``
#: literal that ``first`` and ``interval`` accept.
DECLARATION_ATTRIBUTES = frozenset({
    "name", "first", "interval", "cutoff", "dimension", "initial", "inf",
})

#: every word the lexer will refuse to hand back as an identifier.
RESERVED_WORDS = (EXPRESSION_KEYWORDS | DECLARATION_KEYWORDS
                  | DECLARATION_ATTRIBUTES)


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------
#: arities that differ in the python-2 era table shipped under
#: ``desmond/packages/enhanced_sampling`` - verified against that source,
#: where ``RMSDSyntax.arg_lengths`` is ``{2, 3}``.
_LEGACY_NARGS: dict = {"rmsd": (2, 3)}


@dataclass(frozen=True)
class Signature:
    """One name the language knows, and the shape rule attached to it.

    ``ret`` and ``args`` use the dimensionality codes described in the module
    docstring: a non-negative ``int`` is a fixed array length, ``-1``/``-2`` are
    wildcards unified across the signature, ``'string'`` is the string type, and
    ``None`` means the rule is not expressible as a code (every ``special``
    form, and the return of ``array``).

    ``nargs`` lists the accepted argument counts; an empty tuple means variadic.
    For ``simple`` signatures it is always ``(len(args),)`` and is stored
    explicitly so a caller can check arity without inspecting ``kind``.
    """

    name: str
    kind: str
    ret: object
    args: tuple
    nargs: tuple
    doc: str = ""
    side_effect: bool = False
    #: ``+ - * / ^`` are threaded operators, not call forms: the grammar's
    #: ``fcnCall : IDENT^ POPEN!`` cannot match them, so ``^(2.0,3.0)`` is a
    #: syntax error even though the type rule exists.  Completion lists and
    #: "did you mean" suggestions must not offer them as functions.
    callable_: bool = True

    @property
    def variadic(self) -> bool:
        """True when any number of arguments is accepted."""
        return not self.nargs

    def accepts_nargs(self, n: int) -> bool:
        """Whether ``n`` arguments is an allowed count for this name."""
        return self.variadic or n in self.nargs


@dataclass(frozen=True)
class Release:
    """The language as one Desmond release accepts it.

    ``validated`` is the field that matters for what the GUI is allowed to
    claim.  It is ``True`` only for a table read out of the signature table that
    a real installation on this machine actually loads at compile time; it is
    ``False`` for the historical table and for the generic union, both of which
    are informed guesses about some *other* installation.

    ``signatures`` is stored by reference and must be treated as read-only;
    :func:`signatures` hands out copies for that reason.
    """

    release: str
    build: str
    source: str
    validated: bool
    signatures: dict
    keywords: frozenset
    notes: str = ""

    #: names the Desmond engine implements that this release's front end will
    #: nonetheless refuse to compile.  Not part of the required constructor
    #: contract, so it carries a default.
    backend_only: frozenset = field(default_factory=frozenset)


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------
# Every row below is transcribed from getFcnSigs() in the 2025-3 FcnTypes.py.
# The 2020-3 copy is identical, so the two releases share this construction.

_STRING = "string"

# name -> (ret, args, doc) for ThreadSyntax entries
_THREAD: tuple = (
    ("sqrt", "square root of every element"),
    ("sin", "sine of every element, argument in radians"),
    ("cos", "cosine of every element, argument in radians"),
    ("acos", "arc cosine of every element, result in radians"),
    ("log", "natural logarithm of every element"),
    ("exp", "exponential of every element"),
    ("sign", "sign of every element"),
    ("mass", "mass in amu of every particle in a gid array"),
)

# name -> doc for BinaryThreadSyntax entries
_BINARY: tuple = (
    ("+", "element-wise sum; a length-1 operand pairs with every element"),
    ("-", "element-wise difference; a length-1 operand broadcasts"),
    ("*", "element-wise product, not a dot product; a length-1 operand "
          "broadcasts"),
    ("/", "element-wise quotient; a length-1 operand broadcasts"),
    ("^", "element-wise power; right-associative in the grammar"),
    ("atan2", "element-wise two-argument arc tangent, result in radians"),
    ("mod", "element-wise modulus"),
)

# (name, ret, args, doc) for SimpleSyntax entries
_SIMPLE: tuple = (
    ("print", -1, (_STRING, -1),
     "record the second argument in the CV output file under the given "
     "label; returns the value unchanged so it can be used inline"),

    ("angle", 1, (3, 3), "angle between two 3-vectors"),
    ("angle_radians", 1, (3, 3),
     "angle between two 3-vectors in radians; ill-conditioned near 0 and pi"),
    ("angle_gid", 1, (1, 1, 1), "angle at the second of three particles"),
    ("angle_gid_radians", 1, (1, 1, 1),
     "angle at the second of three particles, in radians"),
    ("dihedral", 2, (3, 3, 3),
     "dihedral from three 3-vectors; returns a length-2 array, unlike "
     "dihedral_radians which returns a single number"),
    ("dihedral_radians", 1, (3, 3, 3),
     "dihedral angle from three 3-vectors, in radians"),
    ("dihedral_gid", 2, (1, 1, 1, 1),
     "dihedral of four particles; returns a length-2 array"),
    ("dihedral_gid_radians", 1, (1, 1, 1, 1),
     "dihedral angle of four particles, in radians"),

    ("pow", -1, (-1, -2),
     "power; the base fixes the result length and the exponent is "
     "independently sized. Absent from the pre-2020 table, so it is the one "
     "name whose availability actually varies across releases"),
    ("cross", 3, (3, 3), "cross product of two 3-vectors"),
    ("delta", 3, (1, 1),
     "minimum-image displacement vector between two particles"),
    ("dist", 1, (1, 1), "minimum-image distance between two particles"),
    ("dot", 1, (-1, -1), "inner product of two equal-length arrays"),
    ("elem", 1, (-1, 1),
     "single element of an array; the subscript syntax a[i] lowers to this"),
    ("length", 1, (-1,), "number of elements in an array"),
    ("min", 1, (-1,),
     "smallest element of an array. Present in every release recorded here, "
     "contrary to the common claim that it is backend-only"),
    ("max", 1, (-1,), "largest element of an array"),
    ("min_image", 3, (3,),
     "the given displacement wrapped into the nearest periodic image"),
    ("norm2", 1, (-1,), "squared Euclidean length of an array"),
    ("norm", 1, (-1,), "Euclidean length of an array"),
    ("pos", 3, (1,), "position of one particle as a 3-vector"),
    ("sum", 1, (-1,), "sum of the elements of an array"),
    ("time", 1, (), "current chemical time"),
    ("pos_inner_prod", 3, (-1, -1),
     "weighted sum of particle positions; both arrays have one entry per "
     "particle"),
    ("center_of_mass", 3, (-1,),
     "mass-weighted centre of a gid array, minimum-image aware"),
    ("center_of_geometry", 3, (-1,),
     "unweighted centre of a gid array, minimum-image aware"),

    ("ncoordination", 1, (1, 1, 1, -1, -2),
     "coordination number between two gid groups given cutoff and switching "
     "parameters"),
    ("contact_map", 1, (1, -1), "contact-map order parameter over a gid array"),
    ("rad_gyration", 1, (-1,), "radius of gyration of a gid array"),
    ("rmsd_torsion", 1, (-1, -2), "torsional RMSD between two torsion sets"),
    ("helix", 1, (1, -1, -2), "helicity order parameter over two gid arrays"),

    ("gibbs_min", 1, (1, -1),
     "softened minimum of an array at the given temperature; the "
     "differentiable stand-in for min"),
    ("gibbs_max", 1, (1, -1),
     "softened maximum of an array at the given temperature"),
    ("whim", 3, (-1, -1),
     "the three WHIM eigenvalues of a gid array with per-particle weights; "
     "meta.py subscripts the result to build the whim1/whim2/whim3 CVs"),
)

# (name, ret, nargs, side_effect, doc) for the forms with a bespoke rule.
# The first three come from FcnTypes; the rest are intercepted by enhsamp.py
# before the signature table is consulted and have no entry there at all.
#: written as operators in the grammar, so they can never be called by name
_OPERATOR_NAMES = frozenset({"+", "-", "*", "/", "^"})

_SPECIAL: tuple = (
    ("array", None, (), False,
     "concatenate the arguments into one array; the result length is the sum "
     "of the argument lengths, and array() legally yields a length-0 array"),
    ("meta", 1, (3,), True,
     "accumulate a metadynamics kernel: meta(id, array(height, w1..wt), cv) "
     "where cv has length t and the second argument therefore has length "
     "t + 1. Returns the current bias, so the well-tempered idiom nests one "
     "meta call inside another"),
    ("rmsd", 1, (2, 4), False,   # legacy is (2, 3); see _LEGACY_NARGS
     "RMSD of a 3n-long position array against an n-long weight array. The "
     "4-argument form is accepted but its third and fourth arguments are "
     "never type-checked: RMSDSyntax only appends the extra expected type "
     "when it sees 3 arguments, a count its own arity set excludes"),

    ("atomsel", None, (1,), False,
     "expand an ASL string to the gids it selects. Not in the signature "
     "table: enhsamp.resolve_atomsel rewrites it into a literal array of gids "
     "before type checking runs, so it never reaches getFcnSigs"),
    ("load", None, (1,), False,
     "read a static variable by name. Not in the signature table; typed "
     "against the static declarations by enhsamp.FcnCall.get_type. Rarely "
     "written by hand, because a bare mention of a static name is rewritten "
     "into load(\"name\")"),
    ("store", None, (2,), True,
     "write a value into a static variable, which must already be declared "
     "and must have the same length. Not in the signature table"),
    ("declare_meta", None, (), True,
     "header declaration of a metadynamics accumulator; takes the keyword "
     "attributes dimension, cutoff, first, interval, name and initial. A "
     "grammar form, not a call"),
    ("declare_output", None, (), True,
     "header declaration of the CV output file; takes the keyword attributes "
     "name, first and interval. A grammar form, not a call"),
)


def _build_table(*, include_pow: bool, legacy_arities: bool = False) -> dict:
    """Assemble a name -> Signature mapping.

    Two axes differ between the tables observed on this machine: the python-2
    era table under ``desmond/packages`` has no ``pow`` and gives ``rmsd`` the
    arities ``{2, 3}`` rather than ``{2, 4}``.  Both are knobs here rather
    than a general filter, so a third difference cannot be waved through
    unnoticed.
    """
    table: dict = {}

    for name, doc in _THREAD:
        table[name] = Signature(name=name, kind="thread", ret=-1, args=(-1,),
                                nargs=(1,), doc=doc)

    for name, doc in _BINARY:
        table[name] = Signature(name=name, kind="binary_thread", ret=-1,
                                callable_=name not in _OPERATOR_NAMES,
                                args=(-1, -1), nargs=(2,), doc=doc)

    for name, ret, args, doc in _SIMPLE:
        if name == "pow" and not include_pow:
            continue
        table[name] = Signature(name=name, kind="simple", ret=ret,
                                args=tuple(args), nargs=(len(args),), doc=doc,
                                side_effect=(name == "print"))

    for name, ret, nargs, side_effect, doc in _SPECIAL:
        if legacy_arities and name in _LEGACY_NARGS:
            nargs = _LEGACY_NARGS[name]
        table[name] = Signature(name=name, kind="special", ret=ret, args=(),
                                nargs=tuple(nargs), doc=doc,
                                side_effect=side_effect)

    return table


_SCHRODINGER_2025_3 = (
    "/opt/schrodinger2025-3/internal/lib/python3.11/site-packages/schrodinger/"
    "application/desmond/enhanced_sampling/FcnTypes.py")
_SCHRODINGER_2020_3 = (
    "/opt/schrodinger2020-3/internal/lib/python3.6/site-packages/schrodinger/"
    "application/desmond/enhanced_sampling/FcnTypes.py")
_SCHRODINGER_LEGACY = (
    "/opt/schrodinger2025-3/internal/lib/python3.11/site-packages/schrodinger/"
    "application/desmond/packages/enhanced_sampling/FcnTypes.py")

#: verified by writing let(...) into a .pot and watching enhsamp raise
#: KeyError, while the compiler itself emits [let [...] ...] for every block.
_LET_ONLY = frozenset({"let"})

RELEASES: dict = {
    "2025-3": Release(
        release="2025-3",
        build="160",
        source=_SCHRODINGER_2025_3,
        validated=True,
        signatures=_build_table(include_pow=True),
        keywords=EXPRESSION_KEYWORDS,
        backend_only=_LET_ONLY,
        notes=("Read from the installed suite and exercised against its own "
               "compiler: 53 names in getFcnSigs(), plus the five forms "
               "enhsamp.py intercepts. min, max and pow are all present."),
    ),
    "2020-3": Release(
        release="2020-3",
        build="139",
        source=_SCHRODINGER_2020_3,
        validated=True,
        signatures=_build_table(include_pow=True),
        keywords=EXPRESSION_KEYWORDS,
        backend_only=_LET_ONLY,
        notes=("Read from the installed suite. Its signature table and its "
               "mexp.g grammar are byte-for-byte identical to 2025-3, so the "
               "language is unchanged across the two."),
    ),
    "legacy": Release(
        release="legacy",
        build="",
        source=_SCHRODINGER_LEGACY,
        validated=False,
        signatures=_build_table(include_pow=False,
                                legacy_arities=True),
        keywords=EXPRESSION_KEYWORDS,
        backend_only=_LET_ONLY | frozenset({"pow"}),
        notes=("The Python-2 era table, still shipped under packages/ in both "
               "installed suites but imported by nothing. Marked unvalidated "
               "because it is not the table any installation here compiles "
               "against; it is kept as the evidence of what an older front "
               "end rejects, namely pow."),
    ),
    GENERIC_RELEASE: Release(
        release=GENERIC_RELEASE,
        build="",
        source="union of the tables above",
        validated=False,
        signatures=_build_table(include_pow=True),
        keywords=EXPRESSION_KEYWORDS,
        backend_only=_LET_ONLY | frozenset({"pow"}),
        notes=("Fallback for an unknown or absent Desmond installation. The "
               "signature table is the union of everything known, so nothing "
               "is rejected for lack of a version; backend_only is "
               "correspondingly the widest set, because pow may or may not be "
               "accepted by the front end actually installed."),
    ),
}


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------
def _resolve(release: str | None) -> Release:
    """Return the Release for an id, falling back rather than raising.

    An unrecognised id yields the generic table on purpose: a caller holding a
    version string this module has never heard of is exactly the situation the
    fallback exists for, and raising there would turn "newer Desmond" into
    "broken editor".
    """
    if release is None:
        release = DEFAULT_RELEASE
    return RELEASES.get(release) or RELEASES[GENERIC_RELEASE]


def signatures(release: str | None = None) -> dict:
    """All signatures for a release, as a fresh dict the caller may keep."""
    return dict(_resolve(release).signatures)


def get(name: str, release: str | None = None) -> Signature | None:
    """The signature for ``name``, or ``None`` if this registry has no entry.

    ``None`` means unrecognised, never invalid.  See :func:`known`.
    """
    return _resolve(release).signatures.get(name)


def known(name: str, release: str | None = None) -> bool:
    """Whether this registry has an entry for ``name``.

    ``False`` is a statement about the registry, not about the name.  Render it
    as "unrecognised, preserved" and let the official validator have the last
    word; a ``.pot`` written for a newer suite must still open and still save.
    """
    return name in _resolve(release).signatures


def is_side_effecting(name: str, release: str | None = None) -> bool:
    """Whether calling ``name`` does something beyond producing a value.

    True for ``print``, ``store``, ``meta`` and the two ``declare_`` forms.
    A dead-code pass must not drop a binding that reaches one of these, and an
    unrecognised name is treated as pure only because nothing better is known -
    callers that delete statements should require :func:`known` as well.
    """
    sig = _resolve(release).signatures.get(name)
    return bool(sig and sig.side_effect)


def backend_only(release: str | None = None) -> frozenset:
    """Names the engine implements that this release's front end will reject.

    The point of the set is negative: never tell a user that one of these is
    fine.  ``enhsamp`` refuses them with a bare ``KeyError`` before Desmond is
    ever invoked, so an expression using one cannot run however valid it looks.
    """
    return _resolve(release).backend_only


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
#: releases in the order a human wants to read them: newest validated first,
#: then the historical table, then the fallback.
_MATRIX_ORDER = ("2025-3", "2020-3", "legacy", GENERIC_RELEASE)


def capability_matrix() -> list:
    """One JSON-friendly row per known release, for the docs and the GUI.

    ``only_here`` and ``missing_here`` are computed against the union of all
    the tables rather than hard-coded, so the matrix keeps telling the truth
    when a release is added.
    """
    union: set = set()
    for rel in RELEASES.values():
        union |= set(rel.signatures)

    rows: list = []
    for rid in _MATRIX_ORDER:
        rel = RELEASES[rid]
        names = set(rel.signatures)
        others: set = set()
        for other_id, other in RELEASES.items():
            if other_id != rid:
                others |= set(other.signatures)
        by_kind: dict = {}
        for sig in rel.signatures.values():
            by_kind[sig.kind] = by_kind.get(sig.kind, 0) + 1
        rows.append({
            "release": rel.release,
            "build": rel.build,
            "validated": rel.validated,
            "source": rel.source,
            "functions": len(names),
            "simple": by_kind.get("simple", 0),
            "thread": by_kind.get("thread", 0),
            "binary_thread": by_kind.get("binary_thread", 0),
            "special": by_kind.get("special", 0),
            "side_effecting": sorted(n for n, s in rel.signatures.items()
                                     if s.side_effect),
            "keywords": sorted(rel.keywords),
            "backend_only": sorted(rel.backend_only),
            "only_here": sorted(names - others),
            "missing_here": sorted(union - names),
            "notes": rel.notes,
        })
    return rows


# --------------------------------------------------------------------------
# mapping an installation to a release
# --------------------------------------------------------------------------
#: "Schrodinger Suite 2025-3, Build 160" as written in $SCHRODINGER/version.txt
_VERSION_TXT = re.compile(r"(\d{4}-\d+)\s*,?\s*Build\s+(\d+)", re.IGNORECASE)

#: ".../schrodinger2025-3", the usual install directory name
_DIRNAME = re.compile(r"schrodinger[-_ ]?(\d{4}-\d+)", re.IGNORECASE)

#: files and directories that mark a directory as a Schrodinger installation
_INSTALL_MARKERS = ("version.txt", "run", "internal", "mmshare-v7.1")


def installation_version(path: str) -> tuple | None:
    """``(release, build)`` for a ``$SCHRODINGER`` directory, or ``None``.

    ``version.txt`` is preferred because it carries the build number too; the
    directory name is the fallback for an installation whose ``version.txt``
    has been stripped, and yields an empty build string.  ``None`` means the
    path does not look like a Schrodinger installation at all.
    """
    if not path:
        return None
    root = os.path.normpath(os.path.expanduser(str(path)))
    if not os.path.isdir(root):
        return None
    if not any(os.path.exists(os.path.join(root, m)) for m in _INSTALL_MARKERS):
        return None

    version_txt = os.path.join(root, "version.txt")
    try:
        with open(version_txt, "r", encoding="utf-8", errors="replace") as fh:
            match = _VERSION_TXT.search(fh.read(4096))
        if match:
            return (match.group(1), match.group(2))
    except OSError:
        pass

    match = _DIRNAME.search(os.path.basename(root))
    if match:
        return (match.group(1), "")
    return None


def release_for_installation(path: str) -> str | None:
    """Map a ``$SCHRODINGER`` directory to the release id to use for it.

    Returns ``None`` only when the path is not a Schrodinger installation, so
    the caller can say "no Desmond found" rather than guessing.  A real
    installation of a version this registry has no table for returns
    ``"generic"``: the version is known to exist, the table is not, and the
    right answer is the permissive union rather than nothing.
    """
    found = installation_version(path)
    if found is None:
        return None
    release, _build = found
    return release if release in RELEASES else GENERIC_RELEASE


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def format_dim(code: object) -> str:
    """Render one dimensionality code the way a user should read it."""
    if code is None:
        return "?"
    if isinstance(code, str):
        return code
    if code >= 0:
        return str(code)
    return "A" if code == -1 else "B" if code == -2 else "W%d" % -code


def format_signature(sig: Signature) -> str:
    """One-line human rendering, e.g. ``pow(A, B) -> A``."""
    if sig.kind == "special":
        if sig.variadic:
            args = "..."
        elif sig.nargs == (1,):
            args = "1 arg"
        else:
            args = "%s args" % " or ".join(str(n) for n in sig.nargs)
    else:
        args = ", ".join(format_dim(a) for a in sig.args)
    return "%s(%s) -> %s" % (sig.name, args, format_dim(sig.ret))


if __name__ == "__main__":
    print("=" * 78)
    print("capability matrix")
    print("=" * 78)
    head = ("%-9s %-6s %-5s %5s %6s %6s %6s %6s" %
            ("release", "build", "valid", "fns", "simple", "thread", "binary",
             "spec"))
    print(head)
    print("-" * len(head))
    for row in capability_matrix():
        print("%-9s %-6s %-5s %5d %6d %6d %6d %6d" % (
            row["release"], row["build"] or "-", "yes" if row["validated"]
            else "no", row["functions"], row["simple"], row["thread"],
            row["binary_thread"], row["special"]))
    print()
    for row in capability_matrix():
        print("%s (build %s)" % (row["release"], row["build"] or "-"))
        print("  source        : %s" % row["source"])
        print("  keywords      : %s" % " ".join(row["keywords"]))
        print("  side effects  : %s" % " ".join(row["side_effecting"]))
        print("  backend only  : %s" % (" ".join(row["backend_only"]) or "-"))
        print("  only here     : %s" % (" ".join(row["only_here"]) or "-"))
        print("  missing here  : %s" % (" ".join(row["missing_here"]) or "-"))
        print("  notes         : %s" % row["notes"])
        print()

    print("=" * 78)
    print("signature table for release %s" % DEFAULT_RELEASE)
    print("=" * 78)
    table = signatures(DEFAULT_RELEASE)
    head = "%-22s %-14s %-9s %-24s %s" % ("name", "kind", "nargs",
                                          "signature", "side effect")
    print(head)
    print("-" * len(head))
    for name in sorted(table, key=lambda n: (table[n].kind, n)):
        sig = table[name]
        print("%-22s %-14s %-9s %-24s %s" % (
            name, sig.kind,
            "any" if sig.variadic else ",".join(str(n) for n in sig.nargs),
            format_signature(sig), "yes" if sig.side_effect else ""))
    print()
    for name in sorted(table):
        print("%s\n    %s" % (format_signature(table[name]), table[name].doc))

    print()
    print("=" * 78)
    print("lookup behaviour")
    print("=" * 78)
    for name in ("min", "max", "pow", "let", "whim", "atomsel", "frobnicate"):
        for rid in ("2025-3", "legacy"):
            sig = get(name, rid)
            print("%-11s %-8s known=%-5s side_effect=%-5s backend_only=%-5s "
                  "%s" % (name, rid, known(name, rid),
                          is_side_effecting(name, rid),
                          name in backend_only(rid),
                          format_signature(sig) if sig else "-"))
    print()
    print("unknown release id falls back to %r: %d names" %
          (GENERIC_RELEASE, len(signatures("2099-1"))))
    for probe in ("/opt/schrodinger2025-3", "/opt/schrodinger2020-3",
                  "/opt/schrodinger", "/opt", "/nonexistent"):
        print("%-24s version=%-18s release=%s" %
              (probe, installation_version(probe),
               release_for_installation(probe)))
