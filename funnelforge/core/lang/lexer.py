"""Lossless tokenizer for the Desmond M-expression language.

The grammar this follows is the one Desmond itself uses, ANTLR3 source at
``$SCHRODINGER/internal/lib/python3.11/site-packages/schrodinger/application/
desmond/enhanced_sampling/mexp.g``::

    LIT     :  DIGIT+ ( '.' DIGIT* )? ( ('e'|'E') ('+'|'-')? DIGIT+ )? ;
    IDENT   :  ALPHA ( ALPHA | DIGIT | '_' )* ;
    STRING  :  '"' (~'"')* '"' ;
    COMMENT :  '#' (~('\\r' | '\\n'))* NEWLINE ;

Two consequences of that grammar are easy to get wrong and are honoured here:

* a number must start with a digit - ``.5`` is not a literal in this language,
  and ``1.`` is;
* a string has **no escape mechanism** at all: it runs to the next double
  quote.  Preserving the raw span is therefore the only correct treatment.

The defining property of this module is that it throws nothing away::

    "".join(t.text for t in tokenize(src)) == src

Whitespace, newlines and comments are tokens (``TRIVIA``), not gaps between
tokens, so the token stream is a faithful re-encoding of the file and every
later layer can address the source by character offset.

Offsets are character offsets into the *decoded* text.  Byte-level fidelity -
BOM, encoding and line-ending style - belongs to
:class:`funnelforge.core.safety.Encoding`, which is what turns bytes into this
text and back again.
"""

from __future__ import annotations

from dataclasses import dataclass

#: token kinds
WS = "ws"
NEWLINE = "newline"
COMMENT = "comment"
NUMBER = "number"
STRING = "string"
IDENT = "ident"
KEYWORD = "keyword"
OP = "op"
UNKNOWN = "unknown"
EOF_ = "eof"

TRIVIA = frozenset({WS, NEWLINE, COMMENT})

#: reserved words of the grammar; ``declare_meta``/``declare_output`` are
#: written like calls but are header forms, so they are keywords too.
KEYWORDS = frozenset({
    "if", "then", "else", "series", "static",
    "declare_meta", "declare_output",
})

#: ANTLR turns every quoted literal in ``mexp.g`` into a lexer token, so the
#: header's term names are reserved in *every* position, not just inside
#: ``declare_meta``.  Verified against the installed 2025-3 compiler: each of
#: these used as a variable gives "no viable alternative at input '<word>'".
#: They stay :data:`IDENT` here - the parser still needs them as declaration
#: keys - and the parser reports the misuse instead.
RESERVED = frozenset({
    "name", "first", "interval", "cutoff", "dimension", "initial", "inf",
})

#: every operator and punctuation character the grammar defines.  ``**`` is
#: not Desmond; it is lexed so the parser can reject it with a real message
#: instead of two mysterious stars.
OPERATORS = (
    "**", "=", "^", "+", "-", "*", "/",
    "(", ")", "[", "]", "{", "}", ";", ":", ",",
)

_DIGITS = "0123456789"
_ALPHA = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_IDENT_REST = _ALPHA | set(_DIGITS) | {"_"}


@dataclass(frozen=True)
class Token:
    """One lexeme, with the exact text it covers.

    ``start``/``end`` are character offsets into the source, ``line``/``col``
    are 1-based and are what a diagnostic shows the user.
    """

    kind: str
    text: str
    start: int
    end: int
    line: int
    col: int

    @property
    def is_trivia(self) -> bool:
        return self.kind in TRIVIA

    def __repr__(self) -> str:            # pragma: no cover - debugging aid
        return (f"Token({self.kind}, {self.text!r}, "
                f"{self.line}:{self.col})")


class LexProblem(Exception):
    """Raised only for a condition that makes tokenizing impossible.

    Nothing in this language does: an unterminated string or a stray character
    is reported through :func:`tokenize`'s problem list and still produces a
    token, because losing text is never an option.
    """


@dataclass
class LexResult:
    tokens: list
    problems: list          # (code, message, start, end)

    @property
    def text(self) -> str:
        return "".join(t.text for t in self.tokens if t.kind != EOF_)


def tokenize(src: str, *, max_tokens: int = 2_000_000) -> LexResult:
    """Split ``src`` into a lossless token stream.

    Raises :class:`LexProblem` only when ``max_tokens`` is exceeded, which is a
    resource guard rather than a statement about the file.
    """
    toks: list[Token] = []
    problems: list = []
    i = 0
    n = len(src)
    line = 1
    col = 1

    def push(kind: str, start: int, end: int) -> None:
        nonlocal line, col
        text = src[start:end]
        toks.append(Token(kind, text, start, end, line, col))
        nl = text.count("\n")
        if nl:
            line += nl
            col = len(text) - text.rfind("\n")
        else:
            col += len(text)

    while i < n:
        if len(toks) > max_tokens:
            raise LexProblem(
                f"more than {max_tokens} tokens; refusing to continue")
        c = src[i]

        # File readers may retain a decoded UTF-8 BOM. Keep it as trivia so
        # source patches preserve every byte and all subsequent spans remain
        # offsets into the original text. A BOM inside the program is invalid.
        if i == 0 and c == "\ufeff":
            push(WS, i, i + 1)
            i += 1
            continue

        # -- line endings, kept exactly as written (CRLF stays one token)
        if c == "\r":
            j = i + 2 if src.startswith("\r\n", i) else i + 1
            push(NEWLINE, i, j)
            i = j
            continue
        if c == "\n":
            push(NEWLINE, i, i + 1)
            i += 1
            continue

        # -- horizontal whitespace
        if c in " \t\f\v":
            j = i
            while j < n and src[j] in " \t\f\v":
                j += 1
            push(WS, i, j)
            i = j
            continue

        # -- comment: to the end of the line, newline excluded so that the
        #    line ending stays a token of its own and CRLF survives intact
        if c == "#":
            j = i
            while j < n and src[j] not in "\r\n":
                j += 1
            push(COMMENT, i, j)
            i = j
            continue

        # -- string: no escapes in this grammar, so it runs to the next quote
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 1
            if j >= n:
                problems.append(
                    ("SYN002", "unterminated string literal", i, n))
                push(STRING, i, n)
                i = n
                continue
            push(STRING, i, j + 1)
            i = j + 1
            continue

        # -- number: must start with a digit (see the module docstring)
        if c in _DIGITS:
            j = i
            while j < n and src[j] in _DIGITS:
                j += 1
            if j < n and src[j] == ".":
                j += 1
                while j < n and src[j] in _DIGITS:
                    j += 1
            if j < n and src[j] in "eE":
                k = j + 1
                if k < n and src[k] in "+-":
                    k += 1
                if k < n and src[k] in _DIGITS:
                    while k < n and src[k] in _DIGITS:
                        k += 1
                    j = k
                # an 'e' not followed by digits is not part of the number;
                # it starts an identifier, which the parser will reject
            push(NUMBER, i, j)
            i = j
            continue

        # -- identifier or keyword
        if c in _ALPHA:
            j = i
            while j < n and src[j] in _IDENT_REST:
                j += 1
            word = src[i:j]
            push(KEYWORD if word in KEYWORDS else IDENT, i, j)
            i = j
            continue

        # -- operators and punctuation
        for op in OPERATORS:
            if src.startswith(op, i):
                push(OP, i, i + len(op))
                i += len(op)
                break
        else:
            # anything else is preserved verbatim and reported; a leading '.'
            # lands here, which is exactly right for this grammar
            problems.append(
                ("SYN003", f"character {c!r} is not part of the "
                           "M-expression language", i, i + 1))
            push(UNKNOWN, i, i + 1)
            i += 1

    toks.append(Token(EOF_, "", n, n, line, col))
    return LexResult(toks, problems)


def line_starts(src: str) -> list:
    """Offset of the first character of every line, for offset -> line/col."""
    out = [0]
    for i, ch in enumerate(src):
        if ch == "\n":
            out.append(i + 1)
    return out


def position(src: str, offset: int, starts: list | None = None) -> tuple:
    """(line, col), both 1-based, of a character offset."""
    starts = starts if starts is not None else line_starts(src)
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1, offset - starts[lo] + 1
