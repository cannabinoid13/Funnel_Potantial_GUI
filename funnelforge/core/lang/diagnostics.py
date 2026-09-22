"""Diagnostic catalogue and the layered validation-state model.

A potential file is not "valid" or "invalid".  Seven *independent* questions
can be asked about it, they are answered by seven different pieces of
machinery, and each of them can be unanswered:

``syntax``
    Does this interface's own lossless lexer/CST read the file?  A failure
    here is a statement about *this program*, not about Desmond.
``model``
    How much of the file does the structured view understand well enough to
    edit?  Anything it does not understand is preserved verbatim.
``symbols``
    Names, scopes, single assignment, value categories, array lengths and the
    dependency graph that says which definitions actually reach the energy.
``topology``
    Do the atom selections resolve against the ``.cms`` that will be used?
``official``
    What the installed Schrodinger/Desmond parser and type checker say.  This
    is the only authoritative answer and it is usually the slowest one.
``lint``
    Physical and numerical sanity: geometry that degenerates, force constants
    with the wrong sign, parameters that are not finite.
``package``
    Consistency across the ``.pot``, ``.cms``, ``.msj`` and ``.cfg`` that make
    up one job.

Four distinct concepts, four code families, deliberately never merged
-------------------------------------------------------------------

*unsupported*
    Desmond accepts the construct; this interface does not model it.  Family
    ``MOD``, layer ``model``, severity ``warning`` at worst.  It disables
    structured editing of that statement and nothing else.  The file is kept
    byte for byte and stays perfectly runnable.
*invalid*
    The construct is wrong for Desmond.  Families ``SEM`` (what this
    interface can prove) and ``OFF`` (what the engine itself says).  Only
    ``OFF`` is authoritative.
*unused*
    Well formed, understood, and read by nothing - it does not reach the
    final energy expression.  Family ``SEM``, severity ``info``.  Dead code
    is not an error and must never be presented as one.
*unresolved*
    The answer is not known yet, because a name is not defined
    (``SEM001``) or because no topology has been supplied (``TOP004``).
    Unresolved is not invalid.  It is the absence of a result.

The word "Desmond-valid"
------------------------

Only the ``official`` layer may justify it.  :meth:`Report.verdict` refuses to
produce the word until :data:`State.PASSED` is recorded for ``official``
against the *current* source revision, and the official layer's state is set
exclusively by :meth:`Report.mark` - :meth:`Report.add` will not move it, so
no checker can imply the engine ran by dropping a diagnostic into the bucket.
Until then the headline reads "Desmond validation not performed", which is a
statement about what has been done, not about the file.

Because the official answer depends on both the text and the structure, its
revision key is :func:`combined_rev` of the two; changing either makes the
recorded result :data:`State.STALE` rather than silently keeping it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

#: The seven independent layers, in the order the interface presents them.
LAYERS: tuple = (
    "syntax", "model", "symbols", "topology", "official", "lint", "package",
)

#: One line per layer for the UI, phrased as the question the layer answers.
LAYER_PURPOSE: dict = {
    "syntax": "Can this interface read the file losslessly?",
    "model": "How much of the file can it show and edit as structure?",
    "symbols": "Are the names, arities, lengths and dependencies coherent?",
    "topology": "Do the atom selections resolve against the structure?",
    "official": "What does the installed Desmond parser say? (authoritative)",
    "lint": "Is the physics and the arithmetic sane?",
    "package": "Do the .pot, .cms, .msj and .cfg agree with each other?",
}

SEVERITIES: tuple = ("info", "warning", "error")
_SEVERITY_RANK: dict = {"info": 0, "warning": 1, "error": 2}

#: Code-id prefix -> the single layer it belongs to.  ``MTD`` is the one
#: family that spans two layers: a missing ``declare_meta`` is a symbol
#: problem, while "this is not a metadynamics file" is a model observation.
CODE_FAMILIES: dict = {
    "SYN": "syntax",
    "MOD": "model",
    "SEM": "symbols",
    "MTD": None,
    "TOP": "topology",
    "OFF": "official",
    "LNT": "lint",
    "PKG": "package",
}

_MTD_LAYERS = frozenset({"model", "symbols"})


class State(str, Enum):
    """The state of one layer.

    ``NOT_RUN`` and ``STALE`` are first-class results, not a flavour of
    failure: they say the machinery has no current answer, which is the
    honest thing to show instead of a green light.
    """

    PASSED = "passed"
    WARNING = "warning"
    ERROR = "error"
    NOT_RUN = "not_run"
    STALE = "stale"

    @property
    def has_answer(self) -> bool:
        """True when this state reflects a completed run of the layer."""
        return self in (State.PASSED, State.WARNING, State.ERROR)

    @property
    def label(self) -> str:
        return {
            State.PASSED: "passed",
            State.WARNING: "warnings",
            State.ERROR: "failed",
            State.NOT_RUN: "not run",
            State.STALE: "out of date",
        }[self]


#: Order used when rolling several layer states into one headline.
_STATE_RANK: dict = {
    State.PASSED: 0,
    State.NOT_RUN: 1,
    State.STALE: 2,
    State.WARNING: 3,
    State.ERROR: 4,
}


# --------------------------------------------------------------------------
# the catalogue
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Code:
    """A stable diagnostic kind.

    ``id`` is part of the file format of the session and of every log this
    program writes, so it may never be reused for a different meaning; codes
    are retired, not recycled.  ``explain`` is written for the person running
    the simulation, so it says what the finding means for the run and what to
    do about it, never how the checker is implemented.
    """

    id: str
    layer: str
    severity: str
    title: str
    explain: str
    blocks_edit: bool = False
    blocks_export: bool = False

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self.severity]


def _c(cid: str, layer: str, severity: str, title: str, explain: str,
       blocks_edit: bool = False, blocks_export: bool = False) -> Code:
    return Code(cid, layer, severity, title, " ".join(explain.split()),
                blocks_edit, blocks_export)


_CODES: list = [

    # ---- syntax: this interface's own lexer and CST -----------------------
    _c("SYN001", "syntax", "error", "Unexpected token",
       """A token turned up where the grammar cannot use it, so the file
       cannot be read as structure. The text is untouched on disk; fix the
       highlighted position, or keep working in the text editor and let the
       official validator have the last word.""",
       blocks_edit=True, blocks_export=True),
    _c("SYN002", "syntax", "error", "Unterminated string",
       """A double quote opens a string that is never closed, so the rest of
       the file was swallowed as text. This language has no escape
       character: a string runs to the very next quote, and a quote inside
       one is impossible. Add the closing quote.""",
       blocks_edit=True, blocks_export=True),
    _c("SYN003", "syntax", "error", "Character not in the language",
       """This character is not part of the M-expression language. The usual
       causes are a stray punctuation mark, a number written as .5 instead of
       0.5, or a character pasted from a word processor such as a typographic
       minus or a non-breaking space.""",
       blocks_edit=True, blocks_export=True),
    _c("SYN004", "syntax", "error", "'**' is not an operator here",
       """Exponentiation in the M-expression language is '^', not '**'. Note
       that '^' takes an integer literal exponent only; for anything else use
       pow(base, exponent).""",
       blocks_edit=True, blocks_export=True),
    _c("SYN005", "syntax", "error", "Unclosed block",
       """A '{' opens a block that is never closed by '}'. Blocks introduce a
       scope and evaluate to their last expression; an unclosed one runs to
       the end of the file and takes everything after it with it.""",
       blocks_edit=True, blocks_export=True),
    _c("SYN006", "syntax", "error", "Comment not ended by a newline",
       """The file ends in the middle of a comment, with no final line
       ending. Desmond's grammar defines a comment as '#' followed by text
       and then a newline, so the engine cannot close it and rejects the
       whole file with "no viable alternative at character '<EOF>'". This was
       measured against Schrodinger 2025-3, not inferred: a file whose last
       line is an unterminated comment is refused, while a file that merely
       lacks a final newline after ordinary code is accepted. Add a newline
       at the end of the file.""",
       blocks_edit=False, blocks_export=True),
    _c("SYN007", "syntax", "error", "Unbalanced bracket",
       """A '(', '[' or '{' has no matching partner, or a closing bracket
       appears with nothing open. Everything after the mismatch is read in
       the wrong context, so this is usually the only real error even when
       many are reported.""",
       blocks_edit=True, blocks_export=True),
    _c("SYN008", "syntax", "error", "Missing semicolon",
       """Every statement ends with a semicolon, including the last one and
       including the statements inside a block. Without it the next statement
       is read as a continuation of this one.""",
       blocks_edit=True, blocks_export=True),
    _c("SYN009", "syntax", "error", "Malformed number",
       """This does not form a number in the M-expression grammar. A literal
       must start with a digit, so write 0.5 rather than .5, and an exponent
       must have at least one digit after the e, so 1.0e-3 rather than
       1.0e-.""",
       blocks_edit=True, blocks_export=True),
    _c("SYN010", "syntax", "warning", "Nested too deeply to analyse",
       """This expression nests deeper than the analyser will follow, so it
       is kept exactly as written and treated as opaque. That is a limit of
       this interface: Desmond has no such limit and will evaluate the
       expression normally. Structured editing of this statement is
       disabled; the text is safe.""",
       blocks_edit=True, blocks_export=False),
    _c("SYN011", "syntax", "warning", "Number outside double precision",
       """The literal does not survive being read as a double: it overflows
       to infinity, underflows to zero, or carries more digits than a double
       can hold. Whatever the engine stores will differ from what is written
       here.""",
       blocks_edit=False, blocks_export=False),
    _c("SYN012", "syntax", "error", "Text is not valid UTF-8",
       """The bytes on disk are not valid UTF-8, so the file cannot be
       decoded without guessing. Re-save it as UTF-8 (or plain ASCII, which
       is a subset) from the editor that produced it.""",
       blocks_edit=True, blocks_export=True),
    _c("SYN013", "syntax", "warning", "Byte-order mark at the start",
       """The file begins with a UTF-8 byte-order mark. Some tools write one
       invisibly; parsers that expect plain ASCII see it as three junk
       characters before the first statement. The mark is remembered and
       written back unchanged unless it is removed deliberately.""",
       blocks_edit=False, blocks_export=False),
    _c("SYN014", "syntax", "info", "Mixed line endings",
       """The file mixes CRLF and LF line endings, usually after editing on
       both Windows and Linux. Nothing breaks, and both styles are preserved
       exactly where they are, but a diff of this file against another copy
       will be noisier than the real change.""",
       blocks_edit=False, blocks_export=False),
    _c("SYN015", "syntax", "error", "Round-trip check failed",
       """Re-assembling the parsed file did not reproduce the original text
       byte for byte. That is a defect in this interface, not in the file.
       Structured editing is disabled so that saving cannot corrupt the
       original; the text editor remains safe to use.""",
       blocks_edit=True, blocks_export=True),
    _c("SYN016", "syntax", "error", "Unexpected end of file",
       """The file stops in the middle of a construct - typically inside a
       call's argument list, a series header, or an if/then/else that never
       reached its else.""",
       blocks_edit=True, blocks_export=True),
    _c("SYN017", "syntax", "info", "No statements",
       """The file is empty, or contains only comments and whitespace. That
       is legal but defines no potential, so the run would proceed with no
       bias at all.""",
       blocks_edit=False, blocks_export=False),
    _c("SYN018", "syntax", "warning", "Control character in the text",
       """A control character (other than tab, carriage return or newline) is
       embedded in the text. It is invisible in most editors and will confuse
       the engine's parser. A null byte usually means the file is truncated
       or is not really a text file.""",
       blocks_edit=False, blocks_export=False),

    # ---- model: coverage of the structured view ---------------------------
    _c("MOD001", "model", "warning", "Construct kept but not modelled",
       """This interface has no structured view of this statement, so it is
       shown and saved exactly as written and cannot be edited through the
       panels. This says nothing about whether Desmond accepts it - it
       almost certainly does. Edit it as text, or leave it alone; either way
       it is preserved character for character.""",
       blocks_edit=True, blocks_export=False),
    _c("MOD002", "model", "info", "Function not modelled here",
       """A genuine Desmond function that this interface does not simulate in
       its preview. The call is preserved and will run normally; only the
       plotted preview of the potential is incomplete, because this term's
       value cannot be computed outside the engine.""",
       blocks_edit=False, blocks_export=False),
    _c("MOD003", "model", "warning", "Unknown function kept verbatim",
       """This function is not in the table of functions known for the
       configured Desmond release. It may exist in a newer or older release,
       or it may be a typo. The call is preserved untouched, so the file
       still runs if the function is real. Check the spelling, and let the
       official validator decide.""",
       blocks_edit=True, blocks_export=False),
    _c("MOD004", "model", "warning", "Opaque term reaches the energy",
       """A part of the file that this interface does not model contributes
       to the final energy expression. Everything the panels show - the
       plotted profile, the well depth, the wall positions - is therefore
       incomplete. The exported file is still correct; it is the preview
       that cannot be trusted as the whole picture.""",
       blocks_edit=False, blocks_export=False),
    _c("MOD005", "model", "warning", "Low model coverage",
       """Only a small fraction of the statements have a structured view. The
       file is safe to keep and to run, but most of the editing has to be
       done as text. This typically means the file was written by hand or by
       another tool with a different layout.""",
       blocks_edit=False, blocks_export=False),
    _c("MOD006", "model", "info", "Fully modelled",
       """Every statement has a structured view, so anything in the file can
       be edited through the panels and written back without touching the
       text.""",
       blocks_edit=False, blocks_export=False),
    _c("MOD007", "model", "info", "Layout would change on rewrite",
       """The file is understood, but re-emitting it from the model would
       produce a different layout - different indentation, comment placement
       or number formatting. Values that are not edited keep their original
       text, so this only matters if the whole file is regenerated.""",
       blocks_edit=False, blocks_export=False),
    _c("MOD008", "model", "info", "Not a recognised funnel template",
       """The file does not match the funnel-potential template this
       interface builds, so the funnel panels have nothing to bind to. It is
       still read, checked and edited as a general potential file.""",
       blocks_edit=False, blocks_export=False),

    # ---- symbols: names, scopes, types, dependency graph ------------------
    _c("SEM001", "symbols", "error", "Undefined name",
       """This name is used but never defined before this point. In the
       M-expression language a name must be assigned earlier in the same
       scope or in an enclosing one; there are no forward references. Check
       for a typo, or move the definition above its first use.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM002", "symbols", "error", "Name assigned twice",
       """The language is single assignment: a name may be bound only once
       per scope, so this second assignment is an error rather than an
       update. If a value has to change, give it a new name, or compute it in
       one expression.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM003", "symbols", "error", "Circular definition",
       """These definitions depend on one another in a cycle, so no order
       exists in which they can be evaluated. Break the loop by expressing
       one of them directly in terms of the coordinates.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM004", "symbols", "warning", "Dependency graph too deep",
       """The dependency walk hit its depth limit, so the set of definitions
       that reach the final energy is incomplete. Anything reported about
       unused definitions below this point is unreliable - a name may be
       listed as unused when it is not. This is a limit of the analyser, not
       a problem with the file.""",
       blocks_edit=False, blocks_export=False),
    _c("SEM005", "symbols", "error", "Used before it is defined",
       """The name is defined in this scope, but later than the place it is
       used. Statements are evaluated in order, so move the definition
       above.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM006", "symbols", "info", "Definition is never used",
       """This name is defined correctly but nothing reads it, and it does
       not reach the final energy or any printed quantity, so it has no
       effect on the run. That is often deliberate - a term kept for
       reference or temporarily disconnected - so it is reported for
       information only. Add it to a print() to see it in the CV output.""",
       blocks_edit=False, blocks_export=False),
    _c("SEM007", "symbols", "error", "Array lengths cannot be combined",
       """Every value in this language is an array, and arithmetic pairs
       elements one by one; two arrays can be combined only when they have
       the same length or one of them has length 1. Combining a 3-vector
       with a 2-element array has no meaning. Check whether one operand
       should have been reduced first with sum(), norm() or a subscript.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM008", "symbols", "error", "Subscript out of range",
       """The subscript is outside the array. Subscripts are 0-based, so the
       last element of an array of length n is [n-1]. When looping, write
       series (i = 0 : length(g)), whose upper bound is exclusive.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM009", "symbols", "error", "Iterator used outside its series",
       """A series iterator exists only inside the body of that series. Using
       the name outside it refers to nothing, or worse, silently picks up an
       unrelated variable with the same name.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM010", "symbols", "error", "Wrong number of arguments",
       """This function does not take that many arguments. Extra or missing
       arguments are usually a misplaced comma or a bracket closed one
       argument too early.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM011", "symbols", "error", "Atom selection used as a number",
       """An atom selection was given where a number is required. A selection
       has to be turned into a number first - by center_of_mass(), pos(),
       dist(), length() or a similar function - before it can take part in
       arithmetic.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM012", "symbols", "error", "Number used as an atom selection",
       """A number was given where an atom selection is required. Selections
       come from atomsel("..."), possibly subscripted, and cannot be written
       as bare atom indices.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM013", "symbols", "warning", "Static variable never stored",
       """This static variable is declared, and read, but no store() ever
       writes to it, so it stays at zero for the whole run. A static is only
       useful in pairs: store() to write, the bare name to read the value
       from the previous step.""",
       blocks_edit=False, blocks_export=False),
    _c("SEM014", "symbols", "warning", "Name shadows an outer definition",
       """A name inside this block or series hides one with the same name
       outside it. Everything after this point in the inner scope sees the
       inner value, which is easy to misread when the two mean different
       things. Rename one of them.""",
       blocks_edit=False, blocks_export=False),
    _c("SEM015", "symbols", "error", "Block does not end in an expression",
       """A block evaluates to its last statement, so the last statement must
       be an expression and not an assignment. Repeat the name of the result
       as a final statement.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM016", "symbols", "error", "Function does not exist in Desmond",
       """This function is not part of the M-expression language, and there
       is a documented way to write what it does. Common cases: abs(x) is
       x*sign(x); min and max are gibbs_min/gibbs_max, or an if expression;
       tan(x) is sin(x)/cos(x). Unlike an unknown name, this one is known to
       be wrong.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM017", "symbols", "error", "Exponent must be an integer literal",
       """The '^' operator raises to a literal whole-number power only. For a
       fractional or computed exponent use pow(base, exponent), which is
       defined for a positive base.""",
       blocks_edit=False, blocks_export=True),
    _c("SEM018", "symbols", "warning", "Empty or reversed series range",
       """The lower bound of this series is not below its upper bound, so the
       body never runs and the series contributes exactly zero. Remember the
       upper bound is exclusive: 0 : length(g) visits every atom, 0 : 0
       visits none.""",
       blocks_edit=False, blocks_export=False),

    # ---- metadynamics, split across the model and symbol layers -----------
    _c("MTD001", "model", "info", "No meta() call",
       """Nothing in this file deposits hills, so it is a plain biasing
       potential rather than a metadynamics run. That is a perfectly good
       potential file; the metadynamics panels simply have nothing to
       show.""",
       blocks_edit=False, blocks_export=False),
    _c("MTD002", "symbols", "warning", "Accumulator index is not a literal",
       """The first argument of this meta() call is computed rather than
       written as a plain number, so this interface cannot tell which
       accumulator it deposits into and cannot check it against the
       declarations. Desmond resolves it at run time; the check is simply
       not available here.""",
       blocks_edit=False, blocks_export=False),
    _c("MTD003", "symbols", "error", "meta() index has no declare_meta",
       """A meta() call uses an accumulator index that no declare_meta
       declares, either because the file declares none at all or because the
       index is past the last one. Each accumulator has to be declared once
       in the header, with its dimension and hill schedule, before any call
       can deposit into it.""",
       blocks_edit=False, blocks_export=True),
    _c("MTD004", "symbols", "error", "Dimension does not match the CV array",
       """declare_meta says the accumulator has one dimension count while the
       collective-variable array passed to meta() has a different length.
       They must agree: a 2-D run declares dimension = 2 and passes
       array(cv1, cv2).""",
       blocks_edit=False, blocks_export=True),
    _c("MTD005", "symbols", "error", "Hill array has the wrong length",
       """The second argument of meta() is array(height, width...) and must
       hold exactly one height followed by one width per dimension, so its
       length is dimension + 1. A 2-D run needs array(height, sigma_1,
       sigma_2).""",
       blocks_edit=False, blocks_export=True),
    _c("MTD006", "symbols", "error", "declare_meta dimension is not positive",
       """The dimension of an accumulator is the number of collective
       variables it biases, so it has to be at least 1. A zero or negative
       dimension leaves the accumulator with nothing to bias.""",
       blocks_edit=False, blocks_export=True),
    _c("MTD007", "model", "info", "Well-tempered scaling confirmed",
       """The hill height is scaled by exp(-V_bias/kTemp) with a kTemp
       consistent with the bias factor and temperature recorded in the file,
       so this is a genuine well-tempered run and the deposited bias will
       converge to a fixed fraction of the free-energy surface.""",
       blocks_edit=False, blocks_export=False),
    _c("MTD008", "model", "warning", "Well-tempering not confirmed",
       """Hills are deposited, but this interface cannot see the
       well-tempered scaling factor that damps them as the bias grows.
       Either the file deposits hills of constant height - which never
       converges and keeps pushing the system - or it computes the damping in
       a form not recognised here. Check the hill-height expression before
       reading the result as a free-energy surface.""",
       blocks_edit=False, blocks_export=False),
    _c("MTD009", "model", "info", "Zero-height probe call",
       """This meta() call deposits hills of zero height. That is the
       standard way to read the accumulated bias without adding to it, so it
       is treated as a deliberate probe rather than a mistake.""",
       blocks_edit=False, blocks_export=False),
    _c("MTD010", "symbols", "warning", "declare_meta is never used",
       """An accumulator is declared but no meta() call deposits into it. It
       will produce an empty kernel file and no bias.""",
       blocks_edit=False, blocks_export=False),
    _c("MTD011", "symbols", "error", "Accumulator index used twice",
       """Two meta() calls deposit into the same accumulator index. Their
       hills are added into one surface, which is almost never intended and
       makes the resulting free-energy estimate meaningless. Give the second
       one its own index and its own declare_meta.""",
       blocks_edit=False, blocks_export=True),

    # ---- topology: selections against a .cms ------------------------------
    _c("TOP001", "topology", "error", "Selection matches no atoms",
       """This selection resolves to zero atoms in the supplied structure.
       Desmond refuses an empty group, and any centre of mass computed from
       one is undefined. Check the residue numbering and the chain name
       against the structure actually being used - numbering often shifts
       between a crystal structure and a prepared, solvated system.""",
       blocks_edit=False, blocks_export=True),
    _c("TOP002", "topology", "error", "Atom index outside the structure",
       """The selection names an atom index that does not exist in this
       structure. Indices are 1-based in the selection text and must lie
       within the atom count of the .cms being used.""",
       blocks_edit=False, blocks_export=True),
    _c("TOP003", "topology", "warning", "Selection includes solvent",
       """The selection picks up water, ions or hydrogens. For a centre of
       mass this is usually accidental and shifts the collective variable in
       a way that changes as the solvent moves. Add 'and not water and not
       ion' and, for a heavy-atom centre, 'and not hydrogen'.""",
       blocks_edit=False, blocks_export=False),
    _c("TOP004", "topology", "info", "No structure supplied",
       """No .cms has been loaded, so the selections have not been resolved
       to atoms. This is not a problem with the file: it is a check that has
       not been performed. Load the structure the job will use to see the
       real atom counts.""",
       blocks_edit=False, blocks_export=False),
    _c("TOP005", "topology", "error", "Group given where one atom is needed",
       """pos(), mass() and the two-particle functions take a single
       particle, not a group. Subscript the selection - g[0] - or loop over
       it with series (i = 0 : length(g)).""",
       blocks_edit=False, blocks_export=True),
    _c("TOP006", "topology", "warning", "Group spans the periodic box",
       """The atoms of this group are spread over more than half the box, so
       a centre of geometry computed from raw coordinates lands somewhere
       meaningless once the group wraps. center_of_mass() handles periodic
       images; center_of_geometry() does not and requires the group to stay
       within half a cell.""",
       blocks_edit=False, blocks_export=False),
    _c("TOP007", "topology", "warning", "Structure changed since resolving",
       """The loaded structure is not the one these selections were resolved
       against - the atom count or the structure's own hash differs. Atom
       indices only mean something relative to one system, so the resolved
       groups have to be recomputed before they can be trusted.""",
       blocks_edit=False, blocks_export=False),
    _c("TOP008", "topology", "warning", "Selection text not understood here",
       """The built-in selection engine does not understand this expression,
       so it cannot preview which atoms it picks. Maestro's own ASL is richer
       than the subset implemented here; the string is passed through
       untouched and will be evaluated by Desmond at run time.""",
       blocks_edit=False, blocks_export=False),

    # ---- official: the installed Desmond parser, the only authority -------
    _c("OFF001", "official", "info", "Official validation not performed",
       """The installed Desmond parser has not been run against this text,
       so nothing here may be described as Desmond-valid. Every other check
       in this interface is an independent reimplementation and can disagree
       with the engine in both directions. Run the official validation
       before submitting a long job.""",
       blocks_edit=False, blocks_export=False),
    _c("OFF002", "official", "info", "Accepted by Desmond",
       """The installed Desmond parser and type checker read this exact text
       and accepted it. This is the authoritative answer, and it is the only
       basis on which this interface will call a file Desmond-valid.""",
       blocks_edit=False, blocks_export=False),
    _c("OFF003", "official", "error", "Rejected by Desmond",
       """The installed Desmond parser refused this file. The engine's own
       message is reproduced in the evidence below; it takes precedence over
       anything else this interface reports, including any check that
       passed. The job would fail at start-up with this message.""",
       blocks_edit=False, blocks_export=True),
    _c("OFF004", "official", "warning", "Schrodinger installation not found",
       """No usable Schrodinger installation was found, so the authoritative
       check cannot run at all. The interface's own checks still work and are
       still worth reading, but they are not a substitute. Set SCHRODINGER
       to the installation directory.""",
       blocks_edit=False, blocks_export=False),
    _c("OFF005", "official", "warning", "Official validation timed out",
       """The Desmond parser did not finish within the time allowed. This is
       usually a very large file or a loaded machine rather than a problem
       with the potential. Nothing can be concluded from a timeout - the
       result is unknown, not negative.""",
       blocks_edit=False, blocks_export=False),
    _c("OFF006", "official", "warning", "Official validator crashed",
       """The validation process exited abnormally instead of giving a
       verdict. That may be an installation problem, or it may be a genuine
       defect triggered by this file. The output that was captured is in the
       evidence below. The result is unknown.""",
       blocks_edit=False, blocks_export=False),
    _c("OFF007", "official", "warning", "Untested Desmond release",
       """The installed release is not one this interface has been checked
       against. Its verdict is still authoritative and is still the one to
       believe; only the interface's own function table and layout
       assumptions may be out of date relative to it.""",
       blocks_edit=False, blocks_export=False),
    _c("OFF008", "official", "warning", "Validation is out of date: text",
       """The file has been edited since the official validation ran, so the
       recorded verdict belongs to text that no longer exists. Run it again
       before relying on it.""",
       blocks_edit=False, blocks_export=False),
    _c("OFF009", "official", "warning", "Validation is out of date: system",
       """A different structure has been loaded since the official validation
       ran. Selections are resolved against the topology, so the previous
       verdict does not carry over to this system.""",
       blocks_edit=False, blocks_export=False),
    _c("OFF010", "official", "warning", "Accepted, with warnings",
       """Desmond accepted the file but printed warnings of its own while
       doing so. The job will start. The engine's messages are reproduced
       below and are worth reading before committing to a long run.""",
       blocks_edit=False, blocks_export=False),

    # ---- lint: physical and numerical sanity ------------------------------
    _c("LNT001", "lint", "error", "Axis length is effectively zero",
       """The two points defining this axis are at the same place, or nearly
       so, so its direction is undefined and every projection onto it becomes
       numerical noise. Pick two reference groups that are genuinely apart -
       for a funnel, one deep in the pocket and one out in the solvent.""",
       blocks_edit=False, blocks_export=True),
    _c("LNT002", "lint", "error", "Lower bound is not below the upper bound",
       """The lower limit of this interval is greater than or equal to the
       upper limit, so the region between them is empty or inside out. Any
       wall built from it will push in the wrong direction.""",
       blocks_edit=False, blocks_export=True),
    _c("LNT003", "lint", "error", "Permitted radius is not positive",
       """The allowed radius is zero or negative, so no position satisfies
       the restraint and the bias diverges everywhere. A funnel needs a
       positive radius along its whole length.""",
       blocks_edit=False, blocks_export=True),
    _c("LNT004", "lint", "warning", "Cone and cylinder do not meet",
       """The cone radius at the junction does not match the cylinder radius,
       so the permitted radius jumps at that point. The force is
       discontinuous there, which shows up as a spike in the bias and can
       destabilise the integrator. Match the radii, or move the junction.""",
       blocks_edit=False, blocks_export=False),
    _c("LNT005", "lint", "error", "Negative force constant",
       """A restraint with a negative force constant pushes the system away
       from the target instead of holding it there, without limit. Unless
       this is a deliberate repulsive term written another way, the sign is
       wrong.""",
       blocks_edit=False, blocks_export=True),
    _c("LNT006", "lint", "error", "Parameter is not a finite number",
       """This parameter evaluates to NaN or infinity, which propagates into
       every force computed from it and ends the simulation. It usually comes
       from a division by zero, a square root of a negative number, or a
       logarithm of zero somewhere upstream.""",
       blocks_edit=False, blocks_export=True),
    _c("LNT007", "lint", "warning", "Hill width looks wrong for this CV",
       """The Gaussian width is a large or a very small fraction of the range
       this collective variable explores. Too wide and the free-energy
       surface is smeared past any useful feature; too narrow and the run
       never fills the basin in the available time. A width of roughly a
       tenth to a fifth of the smallest feature of interest is the usual
       starting point.""",
       blocks_edit=False, blocks_export=False),
    _c("LNT008", "lint", "warning", "Hill deposition interval looks wrong",
       """Hills are deposited far more or far less often than usual. Too
       frequent and the bias outruns the system's ability to relax, which
       biases the estimate; too rare and the run wastes most of its time.
       Values around 0.5 to 5 ps are typical.""",
       blocks_edit=False, blocks_export=False),
    _c("LNT009", "lint", "warning", "Temperature looks wrong",
       """The temperature used in the potential is far from the range of
       liquid-water simulations. Check that it is in kelvin, and that it
       matches the thermostat temperature in the .cfg - the well-tempered
       relation only holds if the two agree.""",
       blocks_edit=False, blocks_export=False),
    _c("LNT010", "lint", "warning", "Very large energy at the start",
       """Evaluated at the starting coordinates, this potential is already
       enormous. The system will be kicked hard on the first step and the run
       may blow up before it settles. Check the reference geometry and the
       units of the force constants: the ligand may be starting outside the
       permitted region.""",
       blocks_edit=False, blocks_export=False),
    _c("LNT011", "lint", "warning", "Displacement without min_image",
       """A vector between two particles is computed by subtracting positions
       directly. Under periodic boundaries that gives the wrong answer
       whenever the two are in different images, and the error appears
       intermittently as the system diffuses. Use delta() or dist(), which
       apply the minimum-image convention, or wrap the difference in
       min_image().""",
       blocks_edit=False, blocks_export=False),
    _c("LNT012", "lint", "warning", "Kernel cutoff is small",
       """Gaussians are truncated where the distance exceeds this many
       widths. A small cutoff visibly clips each hill and leaves steps in the
       bias; the default of 9 widths is essentially exact.""",
       blocks_edit=False, blocks_export=False),
    _c("LNT013", "lint", "error", "Bias factor is not above one",
       """The well-tempered bias factor must be greater than 1, since kTemp =
       (gamma - 1) k_B T. At exactly 1 no bias is ever deposited, and below 1
       the hill height grows without bound instead of decaying.""",
       blocks_edit=False, blocks_export=True),
    _c("LNT014", "lint", "warning", "pow() with a non-positive base",
       """pow() is only defined for a positive base; Desmond leaves the rest
       undefined and the result is whatever the underlying library returns.
       For a whole-number exponent use '^', which handles negative bases.""",
       blocks_edit=False, blocks_export=False),
    _c("LNT015", "lint", "warning", "Divisor can reach zero",
       """The denominator of this division can be zero in the region the run
       will actually sample, giving an infinite force at that point. Add a
       small regularising constant, as in sqrt(x*x + eps), which is what the
       generated funnel terms do.""",
       blocks_edit=False, blocks_export=False),
    _c("LNT016", "lint", "info", "Term contributes nothing",
       """The force constant, height or weight of this term is exactly zero,
       so it adds nothing to the energy. That is a normal way to disable a
       term without deleting it, and it is reported only so it is not
       forgotten.""",
       blocks_edit=False, blocks_export=False),
    _c("LNT017", "lint", "warning", "Value may be in the wrong unit",
       """This length is small enough to look like nanometres in a file whose
       other distances are in angstroms. Desmond works in angstroms,
       kcal/mol and picoseconds throughout; a value off by a factor of ten
       here changes the physics rather than causing an error.""",
       blocks_edit=False, blocks_export=False),

    # ---- package: cross-file consistency ----------------------------------
    _c("PKG001", "package", "error", "Referenced file is missing",
       """A file named by this job does not exist at the path given. The job
       will fail at start-up, usually after it has already queued. Paths are
       resolved relative to the job directory, not the directory the
       interface was started from.""",
       blocks_edit=False, blocks_export=True),
    _c("PKG002", "package", "error", "Potential file name mismatch",
       """The .cfg names a different potential file from the one being
       edited. Whatever is submitted, the run will use the file the .cfg
       names, so an edit here would silently have no effect.""",
       blocks_edit=False, blocks_export=True),
    _c("PKG003", "package", "warning", "Output or kernel name mismatch",
       """The kernel and CV output file names do not follow the job name used
       by the rest of the package. The run works, but the outputs land under
       a name the analysis tools do not expect, and a second job in the same
       directory can overwrite them.""",
       blocks_edit=False, blocks_export=False),
    _c("PKG004", "package", "error", "Temperature conflict",
       """The temperature in the potential does not match the thermostat
       temperature in the .cfg. Well-tempered metadynamics assumes the two
       are the same; if they differ, the reconstructed free-energy surface is
       scaled by the wrong factor and the numbers are quietly wrong.""",
       blocks_edit=False, blocks_export=True),
    _c("PKG005", "package", "warning", "File changed since validation",
       """A file in this package has changed since the package was last
       checked, so the recorded result belongs to different content. Check
       it again before submitting.""",
       blocks_edit=False, blocks_export=False),
    _c("PKG006", "package", "error", "Unsafe path in archive",
       """A member of this archive has an absolute path or one that climbs
       out of the extraction directory. Extracting it would write outside the
       job folder. The archive is not trusted and was not extracted.""",
       blocks_edit=False, blocks_export=True),
    _c("PKG007", "package", "error", "Atom counts disagree",
       """The structure file and the resolved selections describe systems
       with different atom counts, so the indices in the potential point at
       different atoms from the ones intended. This is the classic result of
       re-solvating a system after writing the potential.""",
       blocks_edit=False, blocks_export=True),
    _c("PKG008", "package", "warning", "Stage does not enable the bias",
       """No stage in the .msj switches the enhanced-sampling potential on,
       so the production run would proceed as plain molecular dynamics and
       the potential file would never be read. The job completes, wastes the
       allocation, and produces no kernel file.""",
       blocks_edit=False, blocks_export=False),
    _c("PKG009", "package", "error", "Restart kernel file missing",
       """The potential asks to continue from an existing kernel file that
       does not exist. A restart that cannot find its hills starts from an
       empty bias, which is not the run that was intended.""",
       blocks_edit=False, blocks_export=True),
    _c("PKG010", "package", "warning", "Package member unreadable",
       """A file that belongs to this job exists but cannot be read -
       permissions, a broken symbolic link, or a filesystem that is not
       mounted. It cannot be checked, so the package result is
       incomplete.""",
       blocks_edit=False, blocks_export=False),
]

#: Every diagnostic kind this build knows, keyed by its stable id.
CATALOG: dict = {c.id: c for c in _CODES}

#: The four concepts the interface refuses to merge, and where they live.
#: Presented in the UI so the distinction is visible rather than implied.
CONCEPTS: dict = {
    "unsupported": ("Valid for Desmond, not modelled by this interface. "
                    "Disables structured editing only.",
                    ("MOD001", "MOD002", "MOD003", "TOP008")),
    "invalid": ("Wrong for Desmond. Only the official layer is "
                "authoritative.",
                ("SEM016", "SEM002", "SEM010", "OFF003")),
    "unused": ("Well formed and understood, but nothing reads it.",
               ("SEM006", "MTD010", "LNT016")),
    "unresolved": ("No answer yet, because something needed is absent.",
                   ("SEM001", "TOP004", "OFF001", "MTD002")),
}


def _validate_catalog() -> None:
    """Fail at import if the catalogue contradicts its own rules."""
    for cid, code in CATALOG.items():
        if code.id != cid:
            raise ValueError(f"{cid}: key does not match Code.id {code.id!r}")
        if len(cid) != 6 or not cid[:3].isalpha() or not cid[3:].isdigit():
            raise ValueError(f"{cid}: ids are three letters and three digits")
        if code.layer not in LAYERS:
            raise ValueError(f"{cid}: unknown layer {code.layer!r}")
        if code.severity not in SEVERITIES:
            raise ValueError(f"{cid}: unknown severity {code.severity!r}")
        family = cid[:3]
        if family not in CODE_FAMILIES:
            raise ValueError(f"{cid}: unknown code family {family!r}")
        fixed = CODE_FAMILIES[family]
        if fixed is not None and code.layer != fixed:
            raise ValueError(f"{cid}: family {family} belongs to layer "
                             f"{fixed!r}, not {code.layer!r}")
        if fixed is None and code.layer not in _MTD_LAYERS:
            raise ValueError(f"{cid}: metadynamics codes belong to the model "
                             f"or symbols layer, not {code.layer!r}")
        if code.severity == "info" and (code.blocks_edit or
                                        code.blocks_export):
            raise ValueError(f"{cid}: an informational code must not block")
        if not code.title or not code.explain:
            raise ValueError(f"{cid}: title and explain are required")
    for name, (_, ids) in CONCEPTS.items():
        for cid in ids:
            if cid not in CATALOG:
                raise ValueError(f"concept {name!r} cites unknown code {cid}")


_validate_catalog()


def codes_for_layer(layer: str) -> list:
    """Every catalogue entry for one layer, in id order."""
    if layer not in LAYERS:
        raise KeyError(layer)
    return [CATALOG[k] for k in sorted(CATALOG) if CATALOG[k].layer == layer]


_FALLBACKS: dict = {}


def _fallback(code_id: str) -> Code:
    """A stand-in for an id this build does not know.

    Session files outlive releases: a report written by a newer build can
    name a code that has since been renumbered or not yet added.  Showing it
    as an unrecognised warning keeps the old result readable instead of
    losing it or crashing on it.
    """
    code = _FALLBACKS.get(code_id)
    if code is None:
        family = code_id[:3] if len(code_id) >= 3 else ""
        layer = CODE_FAMILIES.get(family) or "model"
        code = Code(
            id=code_id, layer=layer, severity="warning",
            title=f"Unrecognised diagnostic {code_id}",
            explain="This result was produced by a different version of the "
                    "interface, which used a code this build does not know. "
                    "The message below is still what that version reported.")
        _FALLBACKS[code_id] = code
    return code


def spec_for(code_id: str) -> Code:
    """The :class:`Code` for an id, or a stand-in that is never ``None``."""
    return CATALOG.get(code_id) or _fallback(code_id)


# --------------------------------------------------------------------------
# revisions
# --------------------------------------------------------------------------
def source_rev(text: str) -> str:
    """A short stable key for a piece of source text.

    Results are pinned to the text they were computed from; comparing keys is
    what turns a stale answer into :data:`State.STALE` instead of a lie.
    """
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return "sha256:" + digest[:16]


def combined_rev(*parts: str) -> str:
    """One key for a result that depends on several inputs.

    The official layer is the reason this exists: its verdict is only valid
    for one text *and* one topology, so it is recorded against
    ``combined_rev(source_rev(text), topology_key)``.
    """
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return "sha256:" + h.hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# a single finding
# --------------------------------------------------------------------------
@dataclass
class Diagnostic:
    """One occurrence of one :class:`Code`.

    The code carries everything that is true of the *kind* of problem; this
    carries everything true of *this* one - where it is and what the offending
    text was.  ``suggestion`` is filled in only when the fix is unambiguous
    and safe to apply mechanically, because the UI offers it as a one-click
    action.
    """

    code: str
    message: str
    evidence: str = ""
    suggestion: str = ""
    file: str = ""
    line: int = 0
    col: int = 0
    end_line: int = 0
    end_col: int = 0
    span: tuple | None = None
    data: dict = field(default_factory=dict)

    @property
    def spec(self) -> Code:
        return spec_for(self.code)

    @property
    def layer(self) -> str:
        return self.spec.layer

    @property
    def severity(self) -> str:
        return self.spec.severity

    @property
    def title(self) -> str:
        return self.spec.title

    @property
    def explain(self) -> str:
        return self.spec.explain

    @property
    def blocks_edit(self) -> bool:
        return self.spec.blocks_edit

    @property
    def blocks_export(self) -> bool:
        return self.spec.blocks_export

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self.severity]

    def location(self) -> str:
        """Human-readable position, empty when the finding is file-wide."""
        where = self.file
        if self.line:
            pos = f"line {self.line}"
            if self.col:
                pos += f", col {self.col}"
            where = f"{where}:{pos}" if where else pos
        return where

    def __str__(self) -> str:
        at = self.location()
        head = f"{self.code} {self.severity}"
        return f"[{head}] {self.message}" + (f" ({at})" if at else "")

    def to_dict(self) -> dict:
        out = {
            "code": self.code,
            "message": self.message,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "end_col": self.end_col,
            "span": list(self.span) if self.span is not None else None,
            "data": dict(self.data),
        }
        return {k: v for k, v in out.items() if v not in ("", 0, None, {})}

    @classmethod
    def from_dict(cls, d: dict) -> "Diagnostic":
        span = d.get("span")
        return cls(
            code=str(d.get("code", "")),
            message=str(d.get("message", "")),
            evidence=str(d.get("evidence", "")),
            suggestion=str(d.get("suggestion", "")),
            file=str(d.get("file", "")),
            line=int(d.get("line", 0) or 0),
            col=int(d.get("col", 0) or 0),
            end_line=int(d.get("end_line", 0) or 0),
            end_col=int(d.get("end_col", 0) or 0),
            span=tuple(span) if span else None,
            data=dict(d.get("data") or {}),
        )


def make(code_id: str, message: str, **kw) -> Diagnostic:
    """Build a :class:`Diagnostic`, refusing an id that is not catalogued.

    Every diagnostic the program raises has to be documented, so an unknown
    id is a programming error and is raised as one.  Reading an old session
    file goes through :meth:`Diagnostic.from_dict`, which tolerates it.
    """
    if code_id not in CATALOG:
        raise KeyError(f"{code_id} is not in the diagnostic catalogue")
    return Diagnostic(code=code_id, message=message, **kw)


# --------------------------------------------------------------------------
# per-layer state
# --------------------------------------------------------------------------
@dataclass
class LayerStatus:
    """What one layer currently knows, and what it knows it about.

    ``source_rev`` is the whole point: a state without the revision it was
    computed for cannot be told apart from a state that is out of date.
    """

    layer: str
    state: State = State.NOT_RUN
    diagnostics: list = field(default_factory=list)
    note: str = ""
    ran_at: str = ""
    source_rev: str = ""

    def counts(self) -> dict:
        out = {"info": 0, "warning": 0, "error": 0}
        for d in self.diagnostics:
            out[d.severity] += 1
        return out

    def worst(self) -> str:
        """The highest severity present, or ``''`` when there is nothing."""
        best = ""
        rank = -1
        for d in self.diagnostics:
            if d.rank > rank:
                rank, best = d.rank, d.severity
        return best

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "state": self.state.value,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "note": self.note,
            "ran_at": self.ran_at,
            "source_rev": self.source_rev,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LayerStatus":
        try:
            state = State(d.get("state", State.NOT_RUN.value))
        except ValueError:
            state = State.NOT_RUN
        return cls(
            layer=str(d.get("layer", "")),
            state=state,
            diagnostics=[Diagnostic.from_dict(x)
                         for x in d.get("diagnostics") or []],
            note=str(d.get("note", "")),
            ran_at=str(d.get("ran_at", "")),
            source_rev=str(d.get("source_rev", "")),
        )


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------
#: Bumped when the on-disk shape of :meth:`Report.to_dict` changes.
SCHEMA_VERSION = 1


class Report:
    """The seven layer states together, with no single overall boolean.

    Deliberately absent: anything that reduces the seven to one flag.  The
    closest thing is :meth:`verdict`, and it is a sentence precisely because
    a light cannot say "the file parses here, the engine has not looked at
    it, and no structure is loaded" - which is the normal state of affairs
    while someone is working.
    """

    def __init__(self, layers: dict | None = None):
        self.layers: dict = {
            name: LayerStatus(name) for name in LAYERS
        }
        if layers:
            for name, status in layers.items():
                if name in self.layers:
                    self.layers[name] = status

    # -- recording ------------------------------------------------------
    def add(self, d: Diagnostic) -> None:
        """File a diagnostic under its layer and escalate that layer.

        The ``official`` layer is exempt from the escalation: only the
        process that actually ran the engine may set its state, through
        :meth:`mark`.  Otherwise merely noting "the engine has not run"
        would move it off :data:`State.NOT_RUN` and the interface would
        start implying an authority it does not have.
        """
        layer = d.layer
        status = self.layers.get(layer)
        if status is None:
            raise KeyError(f"{d.code} names unknown layer {layer!r}")
        status.diagnostics.append(d)
        if layer == "official" or status.state is State.STALE:
            return
        if d.severity == "error":
            status.state = State.ERROR
        elif d.severity == "warning" and status.state is not State.ERROR:
            status.state = State.WARNING
        elif status.state is State.NOT_RUN:
            status.state = State.PASSED

    def mark(self, layer: str, state: State, note: str = "",
             source_rev: str = "") -> None:
        """Record the outcome of running one layer.

        Marking a layer :data:`State.NOT_RUN` also clears its provenance, so
        a layer that has been reset can never be mistaken for one whose
        answer merely happens to be old.
        """
        status = self.layers[layer]
        status.state = State(state)
        status.note = note
        if status.state is State.NOT_RUN:
            status.ran_at = ""
            status.source_rev = ""
        else:
            status.ran_at = _now()
            status.source_rev = source_rev

    def reset(self, layer: str) -> None:
        """Clear one layer before re-running it."""
        self.layers[layer] = LayerStatus(layer)

    def stale(self, current_rev: str, revs: dict | None = None) -> None:
        """Mark every layer whose answer belongs to different input.

        A layer that has never run stays :data:`State.NOT_RUN`; there is
        nothing to invalidate.  A layer that ran without recording what it
        ran against is treated as stale, because an unprovable result is
        worth exactly as much as an old one.

        Not every layer depends on the same input.  ``official`` and
        ``topology`` also depend on the structure, so they record a
        :func:`combined_rev` and their current key has to be supplied in
        ``revs``, keyed by layer name; ``current_rev`` covers the rest.  A
        layer whose key is not supplied is compared against ``current_rev``
        and so goes stale whenever it recorded something else - the wrong
        answer in the safe direction, since it only ever asks for a re-run.
        """
        revs = revs or {}
        for name, status in self.layers.items():
            if not status.state.has_answer:
                continue
            if status.source_rev != revs.get(name, current_rev):
                status.state = State.STALE
                status.note = ("result belongs to a different revision of the "
                               "input; run this check again")

    # -- reading --------------------------------------------------------
    def state_of(self, layer: str) -> State:
        return self.layers[layer].state

    def all(self) -> list:
        """Every diagnostic, in layer order."""
        out: list = []
        for name in LAYERS:
            out.extend(self.layers[name].diagnostics)
        return out

    def counts(self) -> dict:
        out = {"info": 0, "warning": 0, "error": 0}
        for d in self.all():
            out[d.severity] += 1
        return out

    def blocks_export(self) -> list:
        """Findings that make a run-ready export refuse to write."""
        return [d for d in self.all() if d.blocks_export]

    def blocks_edit(self) -> list:
        """Findings that make structured editing unsafe."""
        return [d for d in self.all() if d.blocks_edit]

    def summary(self) -> str:
        """One line per layer, readable without colour or icons."""
        rows = []
        for name in LAYERS:
            st = self.layers[name]
            c = st.counts()
            tally = ", ".join(
                f"{c[s]} {s}" + ("s" if c[s] != 1 and s != "info" else "")
                for s in SEVERITIES if c[s])
            when = st.ran_at or "never"
            rev = st.source_rev.replace("sha256:", "") or "-"
            line = (f"{name:<9} {st.state.value.upper():<8} "
                    f"{tally or 'nothing to report':<34} "
                    f"{when:<21} {rev}")
            rows.append(line.rstrip())
            if st.note:
                rows.append(f"{'':<9} note: {st.note}")
        return "\n".join(rows)

    def verdict(self) -> str:
        """The honest headline: what Desmond said, then what this build found.

        The first clause is always about the official layer, because that is
        the only one entitled to an opinion on validity, and it names its own
        absence when it has none.
        """
        off = self.layers["official"]
        if off.state is State.PASSED:
            head = ("Desmond-valid: the official parser accepted this exact "
                    "text")
        elif off.state is State.WARNING:
            head = "Desmond accepted this file, with warnings from the engine"
        elif off.state is State.ERROR:
            head = "Desmond rejected this file"
        elif off.state is State.STALE:
            head = ("Desmond validation is out of date - it ran against "
                    "different text or a different structure")
        else:
            head = "Desmond validation not performed"

        own = [n for n in LAYERS if n != "official"]
        errors = sum(len([d for d in self.layers[n].diagnostics
                          if d.severity == "error"]) for n in own)
        warns = sum(len([d for d in self.layers[n].diagnostics
                         if d.severity == "warning"]) for n in own)
        pending = [n for n in own
                   if self.layers[n].state is State.NOT_RUN]
        outdated = [n for n in own if self.layers[n].state is State.STALE]

        bits = []
        if errors:
            bits.append(f"{errors} error" + ("s" if errors != 1 else ""))
        if warns:
            bits.append(f"{warns} warning" + ("s" if warns != 1 else ""))
        if not bits:
            bits.append("nothing to report")
        tail = "This interface: " + ", ".join(bits)
        if pending:
            tail += "; not run: " + ", ".join(pending)
        if outdated:
            tail += "; out of date: " + ", ".join(outdated)

        if self.layers["syntax"].state is State.ERROR:
            tail += ("; the file does not parse here, which is a limit of "
                     "this interface and not proof that Desmond would "
                     "reject it")
        return f"{head}. {tail}."

    # -- persistence ----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "layers": {n: self.layers[n].to_dict() for n in LAYERS},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Report":
        rep = cls()
        for name, raw in (d.get("layers") or {}).items():
            if name in rep.layers:
                status = LayerStatus.from_dict(raw)
                status.layer = name
                rep.layers[name] = status
        return rep

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Report":
        return cls.from_dict(json.loads(text))

    def __repr__(self) -> str:      # pragma: no cover - debugging aid
        states = " ".join(f"{n}={self.layers[n].state.value}" for n in LAYERS)
        return f"<Report {states}>"


# --------------------------------------------------------------------------
# self-demonstration
# --------------------------------------------------------------------------
def _print_catalog() -> None:
    print("=" * 78)
    print(f"DIAGNOSTIC CATALOGUE - {len(CATALOG)} codes over "
          f"{len(LAYERS)} independent layers")
    print("=" * 78)
    for layer in LAYERS:
        codes = codes_for_layer(layer)
        tally = {s: len([c for c in codes if c.severity == s])
                 for s in SEVERITIES}
        print()
        print(f"--- {layer.upper()} ({len(codes)} codes: "
              f"{tally['error']} error, {tally['warning']} warning, "
              f"{tally['info']} info)")
        print(f"    {LAYER_PURPOSE[layer]}")
        for c in codes:
            flags = "".join(("E" if c.blocks_edit else "-",
                             "X" if c.blocks_export else "-"))
            print(f"    {c.id}  {c.severity:<7} [{flags}]  {c.title}")
            for line in _wrap(c.explain, 66):
                print(f"              {line}")
    print()
    print("--- FLAGS: E = disables structured editing, "
          "X = refuses run-ready export")


def _wrap(text: str, width: int) -> list:
    words = text.split()
    lines: list = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines


def _demo() -> None:
    src_v1 = 'z = dist(a, b);\nk = 5.0;\nk*z*z;\n'
    rev1 = source_rev(src_v1)
    topo1 = "cms:4a2b1c9d"

    rep = Report()

    rep.mark("syntax", State.PASSED, "lossless round trip verified", rev1)

    rep.add(make("MOD001",
                 "this interface has no structured view of `v_user`",
                 evidence="v_user = { t = time(); 0.5*t*t };",
                 line=41, col=1, span=(902, 936)))
    rep.add(make("MOD002",
                 "ncoordination() is not simulated in the preview",
                 evidence="ncoordination(g1, g2, 6.0)", line=7, col=11,
                 span=(180, 206)))
    rep.add(make("MOD004",
                 "an unmodelled term is added into the final energy",
                 evidence="v_user", line=41, col=1))
    rep.mark("model", State.WARNING, "10 of 12 statements modelled", rev1)

    rep.add(make("SEM006",
                 "`d_tyr114` is computed but never used",
                 suggestion='print("Tyr114_to_ligCOM_A", d_tyr114);',
                 line=22, col=1))
    rep.add(make("SEM016",
                 "`abs()` does not exist in the M-expression language",
                 evidence="abs(z - z0)",
                 suggestion="(z - z0)*sign(z - z0)", line=28, col=14))
    rep.mark("symbols", State.ERROR, "1 error, 1 note", rev1)

    rep.add(make("TOP004",
                 "no .cms is loaded, so 3 selections are unresolved"))
    rep.mark("topology", State.NOT_RUN,
             "waiting for a structure", "")

    rep.add(make("LNT007",
                 "sigma_z = 0.02 A is 0.1% of the 20 A the CV spans",
                 evidence="sigma_z = 0.02;", line=12, col=1,
                 data={"sigma": 0.02, "range": 20.0}))
    rep.mark("lint", State.WARNING, "1 parameter worth a second look", rev1)

    rep.add(make("PKG003",
                 "kernel file is `run.kerseq` but the job name is `funnel1`",
                 evidence='name = "run.kerseq"', file="funnel1.cfg", line=4))
    rep.mark("package", State.WARNING, "names disagree", rev1)

    print("=" * 78)
    print("DEMONSTRATION 1 - official never run (the normal working state)")
    print("=" * 78)
    print(rep.summary())
    print()
    print("verdict:", rep.verdict())
    print(f"tallies: {rep.counts()}, {len(rep.all())} diagnostics in total")
    print(f"blocks export: {len(rep.blocks_export())} finding(s) "
          f"-> {[d.code for d in rep.blocks_export()]}")
    print(f"blocks edit:   {len(rep.blocks_edit())} finding(s) "
          f"-> {[d.code for d in rep.blocks_edit()]}")
    print("note that MOD001 disables editing but NOT export: unsupported "
          "here is not invalid for Desmond")
    assert "Desmond validation not performed" in rep.verdict()
    assert "Desmond-valid" not in rep.verdict()
    assert [d.code for d in rep.blocks_edit()] == ["MOD001"]
    assert [d.code for d in rep.blocks_export()] == ["SEM016"]

    print()
    print("=" * 78)
    print("DEMONSTRATION 2 - the official layer runs and accepts")
    print("=" * 78)
    off_rev1 = combined_rev(rev1, topo1)
    rep.reset("symbols")
    rep.mark("symbols", State.PASSED, "all names resolve", rev1)
    rep.add(make("OFF002", "Desmond 2025-3 build 160 accepted the file"))
    rep.mark("official", State.PASSED,
             "schrodinger 2025-3 build 160", off_rev1)
    rep.mark("topology", State.PASSED, "3 selections, 812 atoms",
             combined_rev(rev1, topo1))
    print(rep.summary())
    print()
    print("verdict:", rep.verdict())
    assert "Desmond-valid" in rep.verdict()

    print()
    print("=" * 78)
    print("DEMONSTRATION 3 - same text, a different .cms is loaded")
    print("=" * 78)
    topo2 = "cms:77f0e315"
    print(f"text is unchanged ({rev1}); structure {topo1} -> {topo2}")
    rep.stale(rev1, {"official": combined_rev(rev1, topo2),
                     "topology": combined_rev(rev1, topo2)})
    print(rep.summary())
    print()
    print("verdict:", rep.verdict())
    print("only the two layers that depend on the structure went stale; "
          "the text-only layers keep their answers")
    assert rep.state_of("official") is State.STALE
    assert rep.state_of("topology") is State.STALE
    assert rep.state_of("syntax") is State.PASSED
    assert rep.state_of("lint") is State.WARNING
    assert "Desmond-valid" not in rep.verdict()

    print()
    print("=" * 78)
    print("DEMONSTRATION 4 - one character is typed: everything goes STALE")
    print("=" * 78)
    src_v2 = 'z = dist(a, b);\nk = 5.5;\nk*z*z;\n'
    rev2 = source_rev(src_v2)
    print(f"revision was {rev1}, is now {rev2}")
    rep.stale(rev2, {"official": combined_rev(rev2, topo2),
                     "topology": combined_rev(rev2, topo2)})
    print(rep.summary())
    print()
    print("verdict:", rep.verdict())
    assert all(rep.state_of(n) is State.STALE for n in LAYERS)
    assert "Desmond-valid" not in rep.verdict()

    print()
    print("=" * 78)
    print("DEMONSTRATION 5 - a file that will not parse, engine unavailable")
    print("=" * 78)
    bad = Report()
    bad.add(make("SYN002", "the string opened here is never closed",
                 evidence='"protein and resnum 114', line=3, col=17,
                 span=(48, 71)))
    bad.add(make("SYN014", "12 CRLF lines and 40 LF lines"))
    bad.mark("syntax", State.ERROR, "1 fatal, 1 note", source_rev("..."))
    bad.add(make("OFF004", "no SCHRODINGER installation was found"))
    bad.mark("official", State.NOT_RUN, "engine unavailable")
    print(bad.summary())
    print()
    print("verdict:", bad.verdict())
    assert bad.state_of("official") is State.NOT_RUN, \
        "adding OFF004 must not move the official layer off NOT_RUN"
    assert "Desmond-valid" not in bad.verdict()

    print()
    print("=" * 78)
    print("DEMONSTRATION 6 - every State reachable, and round trip to JSON")
    print("=" * 78)
    every = Report()
    every.mark("syntax", State.PASSED, "", "sha256:aaaa000000000000")
    every.mark("model", State.WARNING, "", "sha256:aaaa000000000000")
    every.mark("symbols", State.ERROR, "", "sha256:aaaa000000000000")
    every.mark("topology", State.NOT_RUN)
    every.mark("official", State.PASSED, "", "sha256:bbbb000000000000")
    every.mark("lint", State.PASSED, "", "sha256:aaaa000000000000")
    every.mark("package", State.PASSED, "", "sha256:aaaa000000000000")
    every.add(make("SEM001", "`rho0` is not defined", line=9, col=5))
    every.stale("sha256:aaaa000000000000")
    seen = {every.state_of(n) for n in LAYERS}
    print("states present:", ", ".join(sorted(s.value for s in seen)))
    assert seen == set(State), f"missing {set(State) - seen}"
    print(every.summary())
    print()
    print("verdict:", every.verdict())

    text = every.to_json()
    back = Report.from_json(text)
    assert back.to_dict() == every.to_dict(), "JSON round trip lost data"
    assert back.verdict() == every.verdict()
    print()
    print(f"JSON round trip: {len(text)} chars, identical after reload")

    print()
    print("=" * 78)
    print("DEMONSTRATION 7 - the four concepts, kept apart")
    print("=" * 78)
    for name, (blurb, ids) in CONCEPTS.items():
        print(f"{name:<12} {blurb}")
        for cid in ids:
            c = CATALOG[cid]
            print(f"             {c.id} {c.layer:<9} {c.severity:<7} "
                  f"{c.title}")

    print()
    print("=" * 78)
    print("DEMONSTRATION 8 - unknown ids: make() refuses, from_dict tolerates")
    print("=" * 78)
    try:
        make("ZZZ999", "invented")
    except KeyError as exc:
        print("make('ZZZ999') ->", exc)
    old = Diagnostic.from_dict({"code": "LNT099",
                                "message": "from a newer build",
                                "line": 3})
    print(f"from_dict('LNT099') -> layer={old.layer} "
          f"severity={old.severity} title={old.title!r}")
    print("                        message still readable:", old.message)
    print("                        str():", str(old))

    print()
    print("=" * 78)
    print("DEMONSTRATION 9 - revision keys")
    print("=" * 78)
    print(f"source_rev(v1)                = {rev1}")
    print(f"source_rev(v2)                = {rev2}")
    print(f"combined_rev(v1, topo1)       = {combined_rev(rev1, topo1)}")
    print(f"combined_rev(v2, topo1)       = {combined_rev(rev2, topo1)}")
    print(f"combined_rev(v1, topo2)       = {combined_rev(rev1, topo2)}")
    assert combined_rev(rev1, topo1) != combined_rev(rev1, topo2)
    assert combined_rev(rev1, topo1) == combined_rev(rev1, topo1)

    print()
    print("=" * 78)
    print("DEMONSTRATION 10 - the remaining accessors")
    print("=" * 78)
    st = rep.layers["model"]
    print(f"LayerStatus('model').counts() = {st.counts()}")
    print(f"LayerStatus('model').worst()  = {st.worst()!r}")
    print(f"LayerStatus(fresh).worst()    = {LayerStatus('lint').worst()!r}")
    for s in State:
        print(f"State.{s.name:<8} value={s.value:<8} "
              f"has_answer={str(s.has_answer):<5} label={s.label!r}")
    c = spec_for("LNT007")
    print(f"spec_for('LNT007')  -> {c.id} {c.layer} rank={c.rank} "
          f"{c.title!r}")
    print(f"spec_for('LNT099')  -> {spec_for('LNT099').title!r} (stand-in)")
    print(f"codes_for_layer('official') -> "
          f"{[x.id for x in codes_for_layer('official')]}")
    try:
        codes_for_layer("nonsense")
    except KeyError as exc:
        print("codes_for_layer('nonsense') ->", repr(exc))
    d = make("TOP001", "`site` matches no atoms", file="funnel.pot",
             line=18, col=9, span=(410, 447), data={"selection": "resnum 999"})
    print(f"Diagnostic.location() = {d.location()!r}")
    print(f"Diagnostic.rank       = {d.rank} ({d.severity})")
    print(f"Diagnostic.to_dict()  = {d.to_dict()}")
    assert Diagnostic.from_dict(d.to_dict()) == d
    assert isinstance(Diagnostic.from_dict(d.to_dict()).span, tuple)
    round_trip = LayerStatus.from_dict(st.to_dict())
    assert round_trip.to_dict() == st.to_dict()
    print("Diagnostic and LayerStatus both survive a dict round trip")
    print(f"Report.state_of('lint') = {rep.state_of('lint')}")
    try:
        rep.state_of("nonsense")
    except KeyError as exc:
        print("Report.state_of('nonsense') ->", repr(exc))
    print(f"repr(Report) = {rep!r}")


if __name__ == "__main__":
    _print_catalog()
    print()
    _demo()
