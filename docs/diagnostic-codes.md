# Diagnostic codes

Every finding this program reports — in the desktop workbench, in
`python -m funnelforge.potcheck`, and in the JSON a CI run writes — carries a
six-character code. This page is the lookup table for those codes.

It is generated from `funnelforge.core.lang.diagnostics.CATALOG`, which is the
single definition of the codes; the module refuses to import if the catalogue
contradicts its own rules (`_validate_catalog`). This build has **100 codes**
across **7 layers**.

If you saw a code in the CLI, jump to the
[catalogue](#the-catalogue) and search for the id. If the id is not on this
page, it came from a different build of the interface: `spec_for()` returns a
stand-in titled `Unrecognised diagnostic <id>` rather than dropping the result,
so old session files stay readable.

## What a code is attached to

A code is the *kind* of finding. The `Diagnostic` you actually see adds the
particulars — the message, the line and column, the offending text, and often a
suggested replacement. So `SEM001` always means "undefined name", and the
message tells you *which* name:

```
$ python -m funnelforge.potcheck tests/fixtures/undefined_symbol.pot
== tests/fixtures/undefined_symbol.pot
syntax    PASSED   nothing to report                  2026-08-28T20:35:01Z  383586cdab06...
          note: 144 tokens, 9 statements
model     PASSED   1 info                             2026-08-28T20:35:01Z  383586cdab06...
          note: 9/9 statements modelled (100%), 0 unrecognised function(s)
symbols   ERROR    1 error                            2026-08-28T20:35:01Z  383586cdab06...
          note: 6 names, 6 reach the potential
topology  NOT_RUN  nothing to report                  never                 -
official  NOT_RUN  nothing to report                  never                 -
lint      PASSED   nothing to report                  2026-08-28T20:35:01Z  383586cdab06...
package   NOT_RUN  nothing to report                  never                 -
  ERROR   SEM001      15:15  `ghost_offset` is used but never defined
          evidence: ghost_offset
  verdict: Desmond validation not performed. This interface: 1 error; not run: topology, package.
```

(Revision hashes shortened for width, and the `-- structure` overview the
tool prints after the verdict is omitted.)

What the catalogue fixes for every code is the `Code` dataclass: `id`,
`layer`, `severity` (`info`, `warning`, `error`), a one-line `title`, the
`explain` text reproduced below, and the two boolean flags `blocks_edit` and
`blocks_export`. Everything else about a finding is per-occurrence.

Code ids are stable — the module's own words: codes "are retired, not
recycled", because an id appears in saved session files and in every log the
program writes.

## Seven layers, not one verdict

There is no single "is this file valid" boolean anywhere in the code, and that
is deliberate. From the module docstring:

> A potential file is not "valid" or "invalid".  Seven *independent* questions
> can be asked about it, they are answered by seven different pieces of
> machinery, and each of them can be unanswered:

The seven are listed in [The seven layers at a glance](#the-seven-layers-at-a-glance)
below. Each layer keeps its own state, its own timestamp, and the hash of the input it
ran against. `Report.verdict()` reads them all out as a sentence rather than
collapsing them into a light.

One layer is different from the other six. `official` is the installed
Schrodinger/Desmond parser, and it is the only one entitled to say the file is
Desmond-valid. `Report.add()` will not move the `official` state — only
`Report.mark()`, called by the process that actually ran the engine, can — so
no checker can imply the engine ran by filing a diagnostic into that bucket.
Until the engine has passed the *current* text, the headline reads
`Desmond validation not performed`.

## Five states per layer

`State` has five members, and two of them are not failures:

| State | `label` | `has_answer` | Meaning |
|-------|---------|--------------|---------|
| `PASSED` | passed | yes | The layer ran and found nothing above `info`. |
| `WARNING` | warnings | yes | The layer ran and reported at least one warning. |
| `ERROR` | failed | yes | The layer ran and reported at least one error. |
| `NOT_RUN` | not run | no | The machinery has no answer. Nothing has been invalidated; nothing has been proved. |
| `STALE` | out of date | no | The layer has an answer, but it belongs to a different revision of the input. |

`STALE` is set by `Report.stale()`, which compares each layer's recorded
`source_rev` against the current one. `official` and `topology` depend on the
`.cms` as well as the text, so they record a `combined_rev` of both; changing
either makes the stored result stale rather than silently keeping it. A layer
that ran but recorded no revision is treated as stale too — "an unprovable
result is worth exactly as much as an old one".

## The four distinctions the catalogue keeps apart

This is the part worth reading before the tables. Four things that most tools
merge into "error" are kept as separate code families here. The module says so
itself, verbatim from its docstring:

```
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
```

In practice:

- **unsupported** is a statement about this program. `MOD001` means the panels
  cannot edit that statement; the bytes are preserved and Desmond will almost
  certainly run it. There are **zero** error-severity codes on the `model`
  layer, which is the same claim made structurally.
- **invalid** is a statement about Desmond. The `SEM` family is what this
  interface can prove without the engine; the `OFF` family is what the engine
  said. When they disagree, the engine wins.
- **unused** is `info`. `SEM006` (`Definition is never used`) does not block
  anything, and `_validate_catalog` enforces that no `info` code ever can.
- **unresolved** is the absence of a result, not a negative result. `TOP004`
  (`No structure supplied`) and `OFF001` (`Official validation not performed`)
  are both `info` for exactly this reason — nothing was found to be wrong,
  nothing was checked.

---

## The seven layers at a glance

| # | Layer | Question it answers | Codes | error | warning | info |
|---|-------|---------------------|------:|------:|--------:|-----:|
| 1 | `syntax` | Can this interface read the file losslessly? | 18 | 11 | 5 | 2 |
| 2 | `model` | How much of the file can it show and edit as structure? | 12 | 0 | 5 | 7 |
| 3 | `symbols` | Are the names, arities, lengths and dependencies coherent? | 25 | 18 | 6 | 1 |
| 4 | `topology` | Do the atom selections resolve against the structure? | 8 | 3 | 4 | 1 |
| 5 | `official` | What does the installed Desmond parser say? (authoritative) | 10 | 1 | 7 | 2 |
| 6 | `lint` | Is the physics and the arithmetic sane? | 17 | 6 | 10 | 1 |
| 7 | `package` | Do the .pot, .cms, .msj and .cfg agree with each other? | 10 | 6 | 4 | 0 |
| | **total** | | **100** | **45** | **41** | **14** |

### Code families

A code id is three letters and three digits. The letters name the family, and `CODE_FAMILIES` pins every family to exactly one layer, with one exception.

| Family | Layer | Codes in this build |
|--------|-------|--------------------:|
| `SYN` | `syntax` | 18 |
| `MOD` | `model` | 8 |
| `SEM` | `symbols` | 18 |
| `MTD` | `model` or `symbols` | 11 (model: 4, symbols: 7) |
| `TOP` | `topology` | 8 |
| `OFF` | `official` | 10 |
| `LNT` | `lint` | 17 |
| `PKG` | `package` | 10 |

### `CONCEPTS`: the same four distinctions, as data

The module does not only describe the four distinctions in prose: it exports them as `CONCEPTS`, a dict naming each one and citing example codes. Reproduced from the source:

| Concept | What it means | Cited codes |
|---------|---------------|-------------|
| **unsupported** | Valid for Desmond, not modelled by this interface. Disables structured editing only. | `MOD001`, `MOD002`, `MOD003`, `TOP008` |
| **invalid** | Wrong for Desmond. Only the official layer is authoritative. | `SEM016`, `SEM002`, `SEM010`, `OFF003` |
| **unused** | Well formed and understood, but nothing reads it. | `SEM006`, `MTD010`, `LNT016` |
| **unresolved** | No answer yet, because something needed is absent. | `SEM001`, `TOP004`, `OFF001`, `MTD002` |

One correction to the source: the comment above `CONCEPTS` says "Presented in the UI so the distinction is visible rather than implied." It is not. In this build `CONCEPTS` is read in only two places, both inside `diagnostics.py` - `_validate_catalog()`, which checks that every cited id exists, and `_demo()`, which prints the four groups. No GUI or CLI code imports it. The distinction is real and is carried by the code families and severities; the dict that names it is not yet on screen anywhere.

---

## The catalogue

`blocks edit` and `blocks export` are the two boolean flags on each `Code`. What they actually do is set out in [Which codes stop you doing what](#which-codes-stop-you-doing-what) at the end.

### 1. `syntax` — Can this interface read the file losslessly?

18 codes: 11 error, 5 warning, 2 info.

| Code | Severity | Title | Blocks editing | Blocks export |
|------|----------|-------|----------------|---------------|
| [`SYN001`](#syn001) | error | Unexpected token | yes | yes |
| [`SYN002`](#syn002) | error | Unterminated string | yes | yes |
| [`SYN003`](#syn003) | error | Character not in the language | yes | yes |
| [`SYN004`](#syn004) | error | '\*\*' is not an operator here | yes | yes |
| [`SYN005`](#syn005) | error | Unclosed block | yes | yes |
| [`SYN006`](#syn006) | warning | Comment not ended by a newline | no | no |
| [`SYN007`](#syn007) | error | Unbalanced bracket | yes | yes |
| [`SYN008`](#syn008) | error | Missing semicolon | yes | yes |
| [`SYN009`](#syn009) | error | Malformed number | yes | yes |
| [`SYN010`](#syn010) | warning | Nested too deeply to analyse | yes | no |
| [`SYN011`](#syn011) | warning | Number outside double precision | no | no |
| [`SYN012`](#syn012) | error | Text is not valid UTF-8 | yes | yes |
| [`SYN013`](#syn013) | warning | Byte-order mark at the start | no | no |
| [`SYN014`](#syn014) | info | Mixed line endings | no | no |
| [`SYN015`](#syn015) | error | Round-trip check failed | yes | yes |
| [`SYN016`](#syn016) | error | Unexpected end of file | yes | yes |
| [`SYN017`](#syn017) | info | No statements | no | no |
| [`SYN018`](#syn018) | warning | Control character in the text | no | no |

<a id="syn001"></a>
**`SYN001` — Unexpected token** — *error, blocks editing, blocks export*

> A token turned up where the grammar cannot use it, so the file cannot be read as structure. The text is untouched on disk; fix the highlighted position, or keep working in the text editor and let the official validator have the last word.

<a id="syn002"></a>
**`SYN002` — Unterminated string** — *error, blocks editing, blocks export*

> A double quote opens a string that is never closed, so the rest of the file was swallowed as text. This language has no escape character: a string runs to the very next quote, and a quote inside one is impossible. Add the closing quote.

<a id="syn003"></a>
**`SYN003` — Character not in the language** — *error, blocks editing, blocks export*

> This character is not part of the M-expression language. The usual causes are a stray punctuation mark, a number written as .5 instead of 0.5, or a character pasted from a word processor such as a typographic minus or a non-breaking space.

<a id="syn004"></a>
**`SYN004` — '\*\*' is not an operator here** — *error, blocks editing, blocks export*

> Exponentiation in the M-expression language is '^', not '\*\*'. Note that '^' takes an integer literal exponent only; for anything else use pow(base, exponent).

<a id="syn005"></a>
**`SYN005` — Unclosed block** — *error, blocks editing, blocks export*

> A '{' opens a block that is never closed by '}'. Blocks introduce a scope and evaluate to their last expression; an unclosed one runs to the end of the file and takes everything after it with it.

<a id="syn006"></a>
**`SYN006` — Comment not ended by a newline** — *warning*

> The file ends in the middle of a comment, with no final line ending. Desmond's own grammar defines a comment as '#' followed by text and then a newline, so a comment on the very last line of a file that has no trailing newline can be rejected by the engine even though every editor shows it as fine. Add a newline at the end of the file.

<a id="syn007"></a>
**`SYN007` — Unbalanced bracket** — *error, blocks editing, blocks export*

> A '(', '\[' or '{' has no matching partner, or a closing bracket appears with nothing open. Everything after the mismatch is read in the wrong context, so this is usually the only real error even when many are reported.

<a id="syn008"></a>
**`SYN008` — Missing semicolon** — *error, blocks editing, blocks export*

> Every statement ends with a semicolon, including the last one and including the statements inside a block. Without it the next statement is read as a continuation of this one.

<a id="syn009"></a>
**`SYN009` — Malformed number** — *error, blocks editing, blocks export*

> This does not form a number in the M-expression grammar. A literal must start with a digit, so write 0.5 rather than .5, and an exponent must have at least one digit after the e, so 1.0e-3 rather than 1.0e-.

<a id="syn010"></a>
**`SYN010` — Nested too deeply to analyse** — *warning, blocks editing*

> This expression nests deeper than the analyser will follow, so it is kept exactly as written and treated as opaque. That is a limit of this interface: Desmond has no such limit and will evaluate the expression normally. Structured editing of this statement is disabled; the text is safe.

<a id="syn011"></a>
**`SYN011` — Number outside double precision** — *warning*

> The literal does not survive being read as a double: it overflows to infinity, underflows to zero, or carries more digits than a double can hold. Whatever the engine stores will differ from what is written here.

<a id="syn012"></a>
**`SYN012` — Text is not valid UTF-8** — *error, blocks editing, blocks export*

> The bytes on disk are not valid UTF-8, so the file cannot be decoded without guessing. Re-save it as UTF-8 (or plain ASCII, which is a subset) from the editor that produced it.

<a id="syn013"></a>
**`SYN013` — Byte-order mark at the start** — *warning*

> The file begins with a UTF-8 byte-order mark. Some tools write one invisibly; parsers that expect plain ASCII see it as three junk characters before the first statement. The mark is remembered and written back unchanged unless it is removed deliberately.

<a id="syn014"></a>
**`SYN014` — Mixed line endings** — *info*

> The file mixes CRLF and LF line endings, usually after editing on both Windows and Linux. Nothing breaks, and both styles are preserved exactly where they are, but a diff of this file against another copy will be noisier than the real change.

<a id="syn015"></a>
**`SYN015` — Round-trip check failed** — *error, blocks editing, blocks export*

> Re-assembling the parsed file did not reproduce the original text byte for byte. That is a defect in this interface, not in the file. Structured editing is disabled so that saving cannot corrupt the original; the text editor remains safe to use.

<a id="syn016"></a>
**`SYN016` — Unexpected end of file** — *error, blocks editing, blocks export*

> The file stops in the middle of a construct - typically inside a call's argument list, a series header, or an if/then/else that never reached its else.

<a id="syn017"></a>
**`SYN017` — No statements** — *info*

> The file is empty, or contains only comments and whitespace. That is legal but defines no potential, so the run would proceed with no bias at all.

<a id="syn018"></a>
**`SYN018` — Control character in the text** — *warning*

> A control character (other than tab, carriage return or newline) is embedded in the text. It is invisible in most editors and will confuse the engine's parser. A null byte usually means the file is truncated or is not really a text file.


### 2. `model` — How much of the file can it show and edit as structure?

12 codes: 0 error, 5 warning, 7 info.

| Code | Severity | Title | Blocks editing | Blocks export |
|------|----------|-------|----------------|---------------|
| [`MOD001`](#mod001) | warning | Construct kept but not modelled | yes | no |
| [`MOD002`](#mod002) | info | Function not modelled here | no | no |
| [`MOD003`](#mod003) | warning | Unknown function kept verbatim | yes | no |
| [`MOD004`](#mod004) | warning | Opaque term reaches the energy | no | no |
| [`MOD005`](#mod005) | warning | Low model coverage | no | no |
| [`MOD006`](#mod006) | info | Fully modelled | no | no |
| [`MOD007`](#mod007) | info | Layout would change on rewrite | no | no |
| [`MOD008`](#mod008) | info | Not a recognised funnel template | no | no |
| [`MTD001`](#mtd001) | info | No meta() call | no | no |
| [`MTD007`](#mtd007) | info | Well-tempered scaling confirmed | no | no |
| [`MTD008`](#mtd008) | warning | Well-tempering not confirmed | no | no |
| [`MTD009`](#mtd009) | info | Zero-height probe call | no | no |

<a id="mod001"></a>
**`MOD001` — Construct kept but not modelled** — *warning, blocks editing*

> This interface has no structured view of this statement, so it is shown and saved exactly as written and cannot be edited through the panels. This says nothing about whether Desmond accepts it - it almost certainly does. Edit it as text, or leave it alone; either way it is preserved character for character.

<a id="mod002"></a>
**`MOD002` — Function not modelled here** — *info*

> A genuine Desmond function that this interface does not simulate in its preview. The call is preserved and will run normally; only the plotted preview of the potential is incomplete, because this term's value cannot be computed outside the engine.

<a id="mod003"></a>
**`MOD003` — Unknown function kept verbatim** — *warning, blocks editing*

> This function is not in the table of functions known for the configured Desmond release. It may exist in a newer or older release, or it may be a typo. The call is preserved untouched, so the file still runs if the function is real. Check the spelling, and let the official validator decide.

<a id="mod004"></a>
**`MOD004` — Opaque term reaches the energy** — *warning*

> A part of the file that this interface does not model contributes to the final energy expression. Everything the panels show - the plotted profile, the well depth, the wall positions - is therefore incomplete. The exported file is still correct; it is the preview that cannot be trusted as the whole picture.

<a id="mod005"></a>
**`MOD005` — Low model coverage** — *warning*

> Only a small fraction of the statements have a structured view. The file is safe to keep and to run, but most of the editing has to be done as text. This typically means the file was written by hand or by another tool with a different layout.

<a id="mod006"></a>
**`MOD006` — Fully modelled** — *info*

> Every statement has a structured view, so anything in the file can be edited through the panels and written back without touching the text.

<a id="mod007"></a>
**`MOD007` — Layout would change on rewrite** — *info*

> The file is understood, but re-emitting it from the model would produce a different layout - different indentation, comment placement or number formatting. Values that are not edited keep their original text, so this only matters if the whole file is regenerated.

<a id="mod008"></a>
**`MOD008` — Not a recognised funnel template** — *info*

> The file does not match the funnel-potential template this interface builds, so the funnel panels have nothing to bind to. It is still read, checked and edited as a general potential file.

<a id="mtd001"></a>
**`MTD001` — No meta() call** — *info*

> Nothing in this file deposits hills, so it is a plain biasing potential rather than a metadynamics run. That is a perfectly good potential file; the metadynamics panels simply have nothing to show.

<a id="mtd007"></a>
**`MTD007` — Well-tempered scaling confirmed** — *info*

> The hill height is scaled by exp(-V_bias/kTemp) with a kTemp consistent with the bias factor and temperature recorded in the file, so this is a genuine well-tempered run and the deposited bias will converge to a fixed fraction of the free-energy surface.

<a id="mtd008"></a>
**`MTD008` — Well-tempering not confirmed** — *warning*

> Hills are deposited, but this interface cannot see the well-tempered scaling factor that damps them as the bias grows. Either the file deposits hills of constant height - which never converges and keeps pushing the system - or it computes the damping in a form not recognised here. Check the hill-height expression before reading the result as a free-energy surface.

<a id="mtd009"></a>
**`MTD009` — Zero-height probe call** — *info*

> This meta() call deposits hills of zero height. That is the standard way to read the accumulated bias without adding to it, so it is treated as a deliberate probe rather than a mistake.


### 3. `symbols` — Are the names, arities, lengths and dependencies coherent?

25 codes: 18 error, 6 warning, 1 info.

| Code | Severity | Title | Blocks editing | Blocks export |
|------|----------|-------|----------------|---------------|
| [`MTD002`](#mtd002) | warning | Accumulator index is not a literal | no | no |
| [`MTD003`](#mtd003) | error | meta() index has no declare_meta | no | yes |
| [`MTD004`](#mtd004) | error | Dimension does not match the CV array | no | yes |
| [`MTD005`](#mtd005) | error | Hill array has the wrong length | no | yes |
| [`MTD006`](#mtd006) | error | declare_meta dimension is not positive | no | yes |
| [`MTD010`](#mtd010) | warning | declare_meta is never used | no | no |
| [`MTD011`](#mtd011) | error | Accumulator index used twice | no | yes |
| [`SEM001`](#sem001) | error | Undefined name | no | yes |
| [`SEM002`](#sem002) | error | Name assigned twice | no | yes |
| [`SEM003`](#sem003) | error | Circular definition | no | yes |
| [`SEM004`](#sem004) | warning | Dependency graph too deep | no | no |
| [`SEM005`](#sem005) | error | Used before it is defined | no | yes |
| [`SEM006`](#sem006) | info | Definition is never used | no | no |
| [`SEM007`](#sem007) | error | Array lengths cannot be combined | no | yes |
| [`SEM008`](#sem008) | error | Subscript out of range | no | yes |
| [`SEM009`](#sem009) | error | Iterator used outside its series | no | yes |
| [`SEM010`](#sem010) | error | Wrong number of arguments | no | yes |
| [`SEM011`](#sem011) | error | Atom selection used as a number | no | yes |
| [`SEM012`](#sem012) | error | Number used as an atom selection | no | yes |
| [`SEM013`](#sem013) | warning | Static variable never stored | no | no |
| [`SEM014`](#sem014) | warning | Name shadows an outer definition | no | no |
| [`SEM015`](#sem015) | error | Block does not end in an expression | no | yes |
| [`SEM016`](#sem016) | error | Function does not exist in Desmond | no | yes |
| [`SEM017`](#sem017) | error | Exponent must be an integer literal | no | yes |
| [`SEM018`](#sem018) | warning | Empty or reversed series range | no | no |

<a id="mtd002"></a>
**`MTD002` — Accumulator index is not a literal** — *warning*

> The first argument of this meta() call is computed rather than written as a plain number, so this interface cannot tell which accumulator it deposits into and cannot check it against the declarations. Desmond resolves it at run time; the check is simply not available here.

<a id="mtd003"></a>
**`MTD003` — meta() index has no declare_meta** — *error, blocks export*

> A meta() call uses an accumulator index that no declare_meta declares, either because the file declares none at all or because the index is past the last one. Each accumulator has to be declared once in the header, with its dimension and hill schedule, before any call can deposit into it.

<a id="mtd004"></a>
**`MTD004` — Dimension does not match the CV array** — *error, blocks export*

> declare_meta says the accumulator has one dimension count while the collective-variable array passed to meta() has a different length. They must agree: a 2-D run declares dimension = 2 and passes array(cv1, cv2).

<a id="mtd005"></a>
**`MTD005` — Hill array has the wrong length** — *error, blocks export*

> The second argument of meta() is array(height, width...) and must hold exactly one height followed by one width per dimension, so its length is dimension + 1. A 2-D run needs array(height, sigma_1, sigma_2).

<a id="mtd006"></a>
**`MTD006` — declare_meta dimension is not positive** — *error, blocks export*

> The dimension of an accumulator is the number of collective variables it biases, so it has to be at least 1. A zero or negative dimension leaves the accumulator with nothing to bias.

<a id="mtd010"></a>
**`MTD010` — declare_meta is never used** — *warning*

> An accumulator is declared but no meta() call deposits into it. It will produce an empty kernel file and no bias.

<a id="mtd011"></a>
**`MTD011` — Accumulator index used twice** — *error, blocks export*

> Two meta() calls deposit into the same accumulator index. Their hills are added into one surface, which is almost never intended and makes the resulting free-energy estimate meaningless. Give the second one its own index and its own declare_meta.

<a id="sem001"></a>
**`SEM001` — Undefined name** — *error, blocks export*

> This name is used but never defined before this point. In the M-expression language a name must be assigned earlier in the same scope or in an enclosing one; there are no forward references. Check for a typo, or move the definition above its first use.

<a id="sem002"></a>
**`SEM002` — Name assigned twice** — *error, blocks export*

> The language is single assignment: a name may be bound only once per scope, so this second assignment is an error rather than an update. If a value has to change, give it a new name, or compute it in one expression.

<a id="sem003"></a>
**`SEM003` — Circular definition** — *error, blocks export*

> These definitions depend on one another in a cycle, so no order exists in which they can be evaluated. Break the loop by expressing one of them directly in terms of the coordinates.

<a id="sem004"></a>
**`SEM004` — Dependency graph too deep** — *warning*

> The dependency walk hit its depth limit, so the set of definitions that reach the final energy is incomplete. Anything reported about unused definitions below this point is unreliable - a name may be listed as unused when it is not. This is a limit of the analyser, not a problem with the file.

<a id="sem005"></a>
**`SEM005` — Used before it is defined** — *error, blocks export*

> The name is defined in this scope, but later than the place it is used. Statements are evaluated in order, so move the definition above.

<a id="sem006"></a>
**`SEM006` — Definition is never used** — *info*

> This name is defined correctly but nothing reads it, and it does not reach the final energy or any printed quantity, so it has no effect on the run. That is often deliberate - a term kept for reference or temporarily disconnected - so it is reported for information only. Add it to a print() to see it in the CV output.

<a id="sem007"></a>
**`SEM007` — Array lengths cannot be combined** — *error, blocks export*

> Every value in this language is an array, and arithmetic pairs elements one by one; two arrays can be combined only when they have the same length or one of them has length 1. Combining a 3-vector with a 2-element array has no meaning. Check whether one operand should have been reduced first with sum(), norm() or a subscript.

<a id="sem008"></a>
**`SEM008` — Subscript out of range** — *error, blocks export*

> The subscript is outside the array. Subscripts are 0-based, so the last element of an array of length n is \[n-1\]. When looping, write series (i = 0 : length(g)), whose upper bound is exclusive.

<a id="sem009"></a>
**`SEM009` — Iterator used outside its series** — *error, blocks export*

> A series iterator exists only inside the body of that series. Using the name outside it refers to nothing, or worse, silently picks up an unrelated variable with the same name.

<a id="sem010"></a>
**`SEM010` — Wrong number of arguments** — *error, blocks export*

> This function does not take that many arguments. Extra or missing arguments are usually a misplaced comma or a bracket closed one argument too early.

<a id="sem011"></a>
**`SEM011` — Atom selection used as a number** — *error, blocks export*

> An atom selection was given where a number is required. A selection has to be turned into a number first - by center_of_mass(), pos(), dist(), length() or a similar function - before it can take part in arithmetic.

<a id="sem012"></a>
**`SEM012` — Number used as an atom selection** — *error, blocks export*

> A number was given where an atom selection is required. Selections come from atomsel("..."), possibly subscripted, and cannot be written as bare atom indices.

<a id="sem013"></a>
**`SEM013` — Static variable never stored** — *warning*

> This static variable is declared, and read, but no store() ever writes to it, so it stays at zero for the whole run. A static is only useful in pairs: store() to write, the bare name to read the value from the previous step.

<a id="sem014"></a>
**`SEM014` — Name shadows an outer definition** — *warning*

> A name inside this block or series hides one with the same name outside it. Everything after this point in the inner scope sees the inner value, which is easy to misread when the two mean different things. Rename one of them.

<a id="sem015"></a>
**`SEM015` — Block does not end in an expression** — *error, blocks export*

> A block evaluates to its last statement, so the last statement must be an expression and not an assignment. Repeat the name of the result as a final statement.

<a id="sem016"></a>
**`SEM016` — Function does not exist in Desmond** — *error, blocks export*

> This function is not part of the M-expression language, and there is a documented way to write what it does. Common cases: abs(x) is x\*sign(x); min and max are gibbs_min/gibbs_max, or an if expression; tan(x) is sin(x)/cos(x). Unlike an unknown name, this one is known to be wrong.

<a id="sem017"></a>
**`SEM017` — Exponent must be an integer literal** — *error, blocks export*

> The '^' operator raises to a literal whole-number power only. For a fractional or computed exponent use pow(base, exponent), which is defined for a positive base.

<a id="sem018"></a>
**`SEM018` — Empty or reversed series range** — *warning*

> The lower bound of this series is not below its upper bound, so the body never runs and the series contributes exactly zero. Remember the upper bound is exclusive: 0 : length(g) visits every atom, 0 : 0 visits none.


### 4. `topology` — Do the atom selections resolve against the structure?

8 codes: 3 error, 4 warning, 1 info.

| Code | Severity | Title | Blocks editing | Blocks export |
|------|----------|-------|----------------|---------------|
| [`TOP001`](#top001) | error | Selection matches no atoms | no | yes |
| [`TOP002`](#top002) | error | Atom index outside the structure | no | yes |
| [`TOP003`](#top003) | warning | Selection includes solvent | no | no |
| [`TOP004`](#top004) | info | No structure supplied | no | no |
| [`TOP005`](#top005) | error | Group given where one atom is needed | no | yes |
| [`TOP006`](#top006) | warning | Group spans the periodic box | no | no |
| [`TOP007`](#top007) | warning | Structure changed since resolving | no | no |
| [`TOP008`](#top008) | warning | Selection text not understood here | no | no |

<a id="top001"></a>
**`TOP001` — Selection matches no atoms** — *error, blocks export*

> This selection resolves to zero atoms in the supplied structure. Desmond refuses an empty group, and any centre of mass computed from one is undefined. Check the residue numbering and the chain name against the structure actually being used - numbering often shifts between a crystal structure and a prepared, solvated system.

<a id="top002"></a>
**`TOP002` — Atom index outside the structure** — *error, blocks export*

> The selection names an atom index that does not exist in this structure. Indices are 1-based in the selection text and must lie within the atom count of the .cms being used.

<a id="top003"></a>
**`TOP003` — Selection includes solvent** — *warning*

> The selection picks up water, ions or hydrogens. For a centre of mass this is usually accidental and shifts the collective variable in a way that changes as the solvent moves. Add 'and not water and not ion' and, for a heavy-atom centre, 'and not hydrogen'.

<a id="top004"></a>
**`TOP004` — No structure supplied** — *info*

> No .cms has been loaded, so the selections have not been resolved to atoms. This is not a problem with the file: it is a check that has not been performed. Load the structure the job will use to see the real atom counts.

<a id="top005"></a>
**`TOP005` — Group given where one atom is needed** — *error, blocks export*

> pos(), mass() and the two-particle functions take a single particle, not a group. Subscript the selection - g\[0\] - or loop over it with series (i = 0 : length(g)).

<a id="top006"></a>
**`TOP006` — Group spans the periodic box** — *warning*

> The atoms of this group are spread over more than half the box, so a centre of geometry computed from raw coordinates lands somewhere meaningless once the group wraps. center_of_mass() handles periodic images; center_of_geometry() does not and requires the group to stay within half a cell.

<a id="top007"></a>
**`TOP007` — Structure changed since resolving** — *warning*

> The loaded structure is not the one these selections were resolved against - the atom count or the structure's own hash differs. Atom indices only mean something relative to one system, so the resolved groups have to be recomputed before they can be trusted.

<a id="top008"></a>
**`TOP008` — Selection text not understood here** — *warning*

> The built-in selection engine does not understand this expression, so it cannot preview which atoms it picks. Maestro's own ASL is richer than the subset implemented here; the string is passed through untouched and will be evaluated by Desmond at run time.


### 5. `official` — What does the installed Desmond parser say? (authoritative)

10 codes: 1 error, 7 warning, 2 info.

| Code | Severity | Title | Blocks editing | Blocks export |
|------|----------|-------|----------------|---------------|
| [`OFF001`](#off001) | info | Official validation not performed | no | no |
| [`OFF002`](#off002) | info | Accepted by Desmond | no | no |
| [`OFF003`](#off003) | error | Rejected by Desmond | no | yes |
| [`OFF004`](#off004) | warning | Schrodinger installation not found | no | no |
| [`OFF005`](#off005) | warning | Official validation timed out | no | no |
| [`OFF006`](#off006) | warning | Official validator crashed | no | no |
| [`OFF007`](#off007) | warning | Untested Desmond release | no | no |
| [`OFF008`](#off008) | warning | Validation is out of date: text | no | no |
| [`OFF009`](#off009) | warning | Validation is out of date: system | no | no |
| [`OFF010`](#off010) | warning | Accepted, with warnings | no | no |

<a id="off001"></a>
**`OFF001` — Official validation not performed** — *info*

> The installed Desmond parser has not been run against this text, so nothing here may be described as Desmond-valid. Every other check in this interface is an independent reimplementation and can disagree with the engine in both directions. Run the official validation before submitting a long job.

<a id="off002"></a>
**`OFF002` — Accepted by Desmond** — *info*

> The installed Desmond parser and type checker read this exact text and accepted it. This is the authoritative answer, and it is the only basis on which this interface will call a file Desmond-valid.

<a id="off003"></a>
**`OFF003` — Rejected by Desmond** — *error, blocks export*

> The installed Desmond parser refused this file. The engine's own message is reproduced in the evidence below; it takes precedence over anything else this interface reports, including any check that passed. The job would fail at start-up with this message.

<a id="off004"></a>
**`OFF004` — Schrodinger installation not found** — *warning*

> No usable Schrodinger installation was found, so the authoritative check cannot run at all. The interface's own checks still work and are still worth reading, but they are not a substitute. Set SCHRODINGER to the installation directory.

<a id="off005"></a>
**`OFF005` — Official validation timed out** — *warning*

> The Desmond parser did not finish within the time allowed. This is usually a very large file or a loaded machine rather than a problem with the potential. Nothing can be concluded from a timeout - the result is unknown, not negative.

<a id="off006"></a>
**`OFF006` — Official validator crashed** — *warning*

> The validation process exited abnormally instead of giving a verdict. That may be an installation problem, or it may be a genuine defect triggered by this file. The output that was captured is in the evidence below. The result is unknown.

<a id="off007"></a>
**`OFF007` — Untested Desmond release** — *warning*

> The installed release is not one this interface has been checked against. Its verdict is still authoritative and is still the one to believe; only the interface's own function table and layout assumptions may be out of date relative to it.

<a id="off008"></a>
**`OFF008` — Validation is out of date: text** — *warning*

> The file has been edited since the official validation ran, so the recorded verdict belongs to text that no longer exists. Run it again before relying on it.

<a id="off009"></a>
**`OFF009` — Validation is out of date: system** — *warning*

> A different structure has been loaded since the official validation ran. Selections are resolved against the topology, so the previous verdict does not carry over to this system.

<a id="off010"></a>
**`OFF010` — Accepted, with warnings** — *warning*

> Desmond accepted the file but printed warnings of its own while doing so. The job will start. The engine's messages are reproduced below and are worth reading before committing to a long run.


### 6. `lint` — Is the physics and the arithmetic sane?

17 codes: 6 error, 10 warning, 1 info.

| Code | Severity | Title | Blocks editing | Blocks export |
|------|----------|-------|----------------|---------------|
| [`LNT001`](#lnt001) | error | Axis length is effectively zero | no | yes |
| [`LNT002`](#lnt002) | error | Lower bound is not below the upper bound | no | yes |
| [`LNT003`](#lnt003) | error | Permitted radius is not positive | no | yes |
| [`LNT004`](#lnt004) | warning | Cone and cylinder do not meet | no | no |
| [`LNT005`](#lnt005) | error | Negative force constant | no | yes |
| [`LNT006`](#lnt006) | error | Parameter is not a finite number | no | yes |
| [`LNT007`](#lnt007) | warning | Hill width looks wrong for this CV | no | no |
| [`LNT008`](#lnt008) | warning | Hill deposition interval looks wrong | no | no |
| [`LNT009`](#lnt009) | warning | Temperature looks wrong | no | no |
| [`LNT010`](#lnt010) | warning | Very large energy at the start | no | no |
| [`LNT011`](#lnt011) | warning | Displacement without min_image | no | no |
| [`LNT012`](#lnt012) | warning | Kernel cutoff is small | no | no |
| [`LNT013`](#lnt013) | error | Bias factor is not above one | no | yes |
| [`LNT014`](#lnt014) | warning | pow() with a non-positive base | no | no |
| [`LNT015`](#lnt015) | warning | Divisor can reach zero | no | no |
| [`LNT016`](#lnt016) | info | Term contributes nothing | no | no |
| [`LNT017`](#lnt017) | warning | Value may be in the wrong unit | no | no |

<a id="lnt001"></a>
**`LNT001` — Axis length is effectively zero** — *error, blocks export*

> The two points defining this axis are at the same place, or nearly so, so its direction is undefined and every projection onto it becomes numerical noise. Pick two reference groups that are genuinely apart - for a funnel, one deep in the pocket and one out in the solvent.

<a id="lnt002"></a>
**`LNT002` — Lower bound is not below the upper bound** — *error, blocks export*

> The lower limit of this interval is greater than or equal to the upper limit, so the region between them is empty or inside out. Any wall built from it will push in the wrong direction.

<a id="lnt003"></a>
**`LNT003` — Permitted radius is not positive** — *error, blocks export*

> The allowed radius is zero or negative, so no position satisfies the restraint and the bias diverges everywhere. A funnel needs a positive radius along its whole length.

<a id="lnt004"></a>
**`LNT004` — Cone and cylinder do not meet** — *warning*

> The cone radius at the junction does not match the cylinder radius, so the permitted radius jumps at that point. The force is discontinuous there, which shows up as a spike in the bias and can destabilise the integrator. Match the radii, or move the junction.

<a id="lnt005"></a>
**`LNT005` — Negative force constant** — *error, blocks export*

> A restraint with a negative force constant pushes the system away from the target instead of holding it there, without limit. Unless this is a deliberate repulsive term written another way, the sign is wrong.

<a id="lnt006"></a>
**`LNT006` — Parameter is not a finite number** — *error, blocks export*

> This parameter evaluates to NaN or infinity, which propagates into every force computed from it and ends the simulation. It usually comes from a division by zero, a square root of a negative number, or a logarithm of zero somewhere upstream.

<a id="lnt007"></a>
**`LNT007` — Hill width looks wrong for this CV** — *warning*

> The Gaussian width is a large or a very small fraction of the range this collective variable explores. Too wide and the free-energy surface is smeared past any useful feature; too narrow and the run never fills the basin in the available time. A width of roughly a tenth to a fifth of the smallest feature of interest is the usual starting point.

<a id="lnt008"></a>
**`LNT008` — Hill deposition interval looks wrong** — *warning*

> Hills are deposited far more or far less often than usual. Too frequent and the bias outruns the system's ability to relax, which biases the estimate; too rare and the run wastes most of its time. Values around 0.5 to 5 ps are typical.

<a id="lnt009"></a>
**`LNT009` — Temperature looks wrong** — *warning*

> The temperature used in the potential is far from the range of liquid-water simulations. Check that it is in kelvin, and that it matches the thermostat temperature in the .cfg - the well-tempered relation only holds if the two agree.

<a id="lnt010"></a>
**`LNT010` — Very large energy at the start** — *warning*

> Evaluated at the starting coordinates, this potential is already enormous. The system will be kicked hard on the first step and the run may blow up before it settles. Check the reference geometry and the units of the force constants: the ligand may be starting outside the permitted region.

<a id="lnt011"></a>
**`LNT011` — Displacement without min_image** — *warning*

> A vector between two particles is computed by subtracting positions directly. Under periodic boundaries that gives the wrong answer whenever the two are in different images, and the error appears intermittently as the system diffuses. Use delta() or dist(), which apply the minimum-image convention, or wrap the difference in min_image().

<a id="lnt012"></a>
**`LNT012` — Kernel cutoff is small** — *warning*

> Gaussians are truncated where the distance exceeds this many widths. A small cutoff visibly clips each hill and leaves steps in the bias; the default of 9 widths is essentially exact.

<a id="lnt013"></a>
**`LNT013` — Bias factor is not above one** — *error, blocks export*

> The well-tempered bias factor must be greater than 1, since kTemp = (gamma - 1) k_B T. At exactly 1 no bias is ever deposited, and below 1 the hill height grows without bound instead of decaying.

<a id="lnt014"></a>
**`LNT014` — pow() with a non-positive base** — *warning*

> pow() is only defined for a positive base; Desmond leaves the rest undefined and the result is whatever the underlying library returns. For a whole-number exponent use '^', which handles negative bases.

<a id="lnt015"></a>
**`LNT015` — Divisor can reach zero** — *warning*

> The denominator of this division can be zero in the region the run will actually sample, giving an infinite force at that point. Add a small regularising constant, as in sqrt(x\*x + eps), which is what the generated funnel terms do.

<a id="lnt016"></a>
**`LNT016` — Term contributes nothing** — *info*

> The force constant, height or weight of this term is exactly zero, so it adds nothing to the energy. That is a normal way to disable a term without deleting it, and it is reported only so it is not forgotten.

<a id="lnt017"></a>
**`LNT017` — Value may be in the wrong unit** — *warning*

> This length is small enough to look like nanometres in a file whose other distances are in angstroms. Desmond works in angstroms, kcal/mol and picoseconds throughout; a value off by a factor of ten here changes the physics rather than causing an error.


### 7. `package` — Do the .pot, .cms, .msj and .cfg agree with each other?

10 codes: 6 error, 4 warning, 0 info.

| Code | Severity | Title | Blocks editing | Blocks export |
|------|----------|-------|----------------|---------------|
| [`PKG001`](#pkg001) | error | Referenced file is missing | no | yes |
| [`PKG002`](#pkg002) | error | Potential file name mismatch | no | yes |
| [`PKG003`](#pkg003) | warning | Output or kernel name mismatch | no | no |
| [`PKG004`](#pkg004) | error | Temperature conflict | no | yes |
| [`PKG005`](#pkg005) | warning | File changed since validation | no | no |
| [`PKG006`](#pkg006) | error | Unsafe path in archive | no | yes |
| [`PKG007`](#pkg007) | error | Atom counts disagree | no | yes |
| [`PKG008`](#pkg008) | warning | Stage does not enable the bias | no | no |
| [`PKG009`](#pkg009) | error | Restart kernel file missing | no | yes |
| [`PKG010`](#pkg010) | warning | Package member unreadable | no | no |

<a id="pkg001"></a>
**`PKG001` — Referenced file is missing** — *error, blocks export*

> A file named by this job does not exist at the path given. The job will fail at start-up, usually after it has already queued. Paths are resolved relative to the job directory, not the directory the interface was started from.

<a id="pkg002"></a>
**`PKG002` — Potential file name mismatch** — *error, blocks export*

> The .cfg names a different potential file from the one being edited. Whatever is submitted, the run will use the file the .cfg names, so an edit here would silently have no effect.

<a id="pkg003"></a>
**`PKG003` — Output or kernel name mismatch** — *warning*

> The kernel and CV output file names do not follow the job name used by the rest of the package. The run works, but the outputs land under a name the analysis tools do not expect, and a second job in the same directory can overwrite them.

<a id="pkg004"></a>
**`PKG004` — Temperature conflict** — *error, blocks export*

> The temperature in the potential does not match the thermostat temperature in the .cfg. Well-tempered metadynamics assumes the two are the same; if they differ, the reconstructed free-energy surface is scaled by the wrong factor and the numbers are quietly wrong.

<a id="pkg005"></a>
**`PKG005` — File changed since validation** — *warning*

> A file in this package has changed since the package was last checked, so the recorded result belongs to different content. Check it again before submitting.

<a id="pkg006"></a>
**`PKG006` — Unsafe path in archive** — *error, blocks export*

> A member of this archive has an absolute path or one that climbs out of the extraction directory. Extracting it would write outside the job folder. The archive is not trusted and was not extracted.

<a id="pkg007"></a>
**`PKG007` — Atom counts disagree** — *error, blocks export*

> The structure file and the resolved selections describe systems with different atom counts, so the indices in the potential point at different atoms from the ones intended. This is the classic result of re-solvating a system after writing the potential.

<a id="pkg008"></a>
**`PKG008` — Stage does not enable the bias** — *warning*

> No stage in the .msj switches the enhanced-sampling potential on, so the production run would proceed as plain molecular dynamics and the potential file would never be read. The job completes, wastes the allocation, and produces no kernel file.

<a id="pkg009"></a>
**`PKG009` — Restart kernel file missing** — *error, blocks export*

> The potential asks to continue from an existing kernel file that does not exist. A restart that cannot find its hills starts from an empty bias, which is not the run that was intended.

<a id="pkg010"></a>
**`PKG010` — Package member unreadable** — *warning*

> A file that belongs to this job exists but cannot be read - permissions, a broken symbolic link, or a filesystem that is not mounted. It cannot be checked, so the package result is incomplete.


---

## Which codes stop you doing what

Of the 100 codes, 14 set `blocks_edit`, 45 set `blocks_export`, and 11 set both. The remaining 52 block nothing: they are reported and that is all.

In this build `blocks_export` is set on exactly the 45 error-severity codes and on nothing else. That equivalence is a property of the current catalogue, not a rule the module enforces; `_validate_catalog` only forbids an `info` code from blocking.

### What the two flags actually do

**`blocks_export`** feeds `Report.blocks_export()`, which is the first thing `Document.export_blockers()` collects. `Document.export_run_ready()` calls `export_blockers()` and, unless `force=True`, returns `("", blockers)` without touching the filesystem. In the GUI, `SourceWindow._export()` shows the blockers in a dialog and never opens the save dialog. The refusal happens before any path is chosen, so a refused export cannot leave a partial file behind.

**`blocks_edit`** feeds `Report.blocks_edit()` and is shown to the user: the diagnostics detail pane in `source_window.py` appends the line `blocks structured editing`. It is not, in this build, a gate. Nothing calls `Report.blocks_edit()` outside the module's own self-demonstration, and `Document.apply_edits()` does not consult it. What actually prevents structured editing of an unmodelled construct is the CST: the statement is an opaque node with no structured view for the panels to bind to. The flag records the intent and labels the finding; treat it as documentation of why the panels are empty, not as an enforced lock.

### Both: structured editing off and run-ready export refused (11 codes)

| Code | Layer | Severity | Title |
|------|-------|----------|-------|
| [`SYN001`](#syn001) | syntax | error | Unexpected token |
| [`SYN002`](#syn002) | syntax | error | Unterminated string |
| [`SYN003`](#syn003) | syntax | error | Character not in the language |
| [`SYN004`](#syn004) | syntax | error | '\*\*' is not an operator here |
| [`SYN005`](#syn005) | syntax | error | Unclosed block |
| [`SYN007`](#syn007) | syntax | error | Unbalanced bracket |
| [`SYN008`](#syn008) | syntax | error | Missing semicolon |
| [`SYN009`](#syn009) | syntax | error | Malformed number |
| [`SYN012`](#syn012) | syntax | error | Text is not valid UTF-8 |
| [`SYN015`](#syn015) | syntax | error | Round-trip check failed |
| [`SYN016`](#syn016) | syntax | error | Unexpected end of file |

### Structured editing only (3 codes)

| Code | Layer | Severity | Title |
|------|-------|----------|-------|
| [`MOD001`](#mod001) | model | warning | Construct kept but not modelled |
| [`MOD003`](#mod003) | model | warning | Unknown function kept verbatim |
| [`SYN010`](#syn010) | syntax | warning | Nested too deeply to analyse |

### Run-ready export only (34 codes)

| Code | Layer | Severity | Title |
|------|-------|----------|-------|
| [`LNT001`](#lnt001) | lint | error | Axis length is effectively zero |
| [`LNT002`](#lnt002) | lint | error | Lower bound is not below the upper bound |
| [`LNT003`](#lnt003) | lint | error | Permitted radius is not positive |
| [`LNT005`](#lnt005) | lint | error | Negative force constant |
| [`LNT006`](#lnt006) | lint | error | Parameter is not a finite number |
| [`LNT013`](#lnt013) | lint | error | Bias factor is not above one |
| [`MTD003`](#mtd003) | symbols | error | meta() index has no declare_meta |
| [`MTD004`](#mtd004) | symbols | error | Dimension does not match the CV array |
| [`MTD005`](#mtd005) | symbols | error | Hill array has the wrong length |
| [`MTD006`](#mtd006) | symbols | error | declare_meta dimension is not positive |
| [`MTD011`](#mtd011) | symbols | error | Accumulator index used twice |
| [`OFF003`](#off003) | official | error | Rejected by Desmond |
| [`PKG001`](#pkg001) | package | error | Referenced file is missing |
| [`PKG002`](#pkg002) | package | error | Potential file name mismatch |
| [`PKG004`](#pkg004) | package | error | Temperature conflict |
| [`PKG006`](#pkg006) | package | error | Unsafe path in archive |
| [`PKG007`](#pkg007) | package | error | Atom counts disagree |
| [`PKG009`](#pkg009) | package | error | Restart kernel file missing |
| [`SEM001`](#sem001) | symbols | error | Undefined name |
| [`SEM002`](#sem002) | symbols | error | Name assigned twice |
| [`SEM003`](#sem003) | symbols | error | Circular definition |
| [`SEM005`](#sem005) | symbols | error | Used before it is defined |
| [`SEM007`](#sem007) | symbols | error | Array lengths cannot be combined |
| [`SEM008`](#sem008) | symbols | error | Subscript out of range |
| [`SEM009`](#sem009) | symbols | error | Iterator used outside its series |
| [`SEM010`](#sem010) | symbols | error | Wrong number of arguments |
| [`SEM011`](#sem011) | symbols | error | Atom selection used as a number |
| [`SEM012`](#sem012) | symbols | error | Number used as an atom selection |
| [`SEM015`](#sem015) | symbols | error | Block does not end in an expression |
| [`SEM016`](#sem016) | symbols | error | Function does not exist in Desmond |
| [`SEM017`](#sem017) | symbols | error | Exponent must be an integer literal |
| [`TOP001`](#top001) | topology | error | Selection matches no atoms |
| [`TOP002`](#top002) | topology | error | Atom index outside the structure |
| [`TOP005`](#top005) | topology | error | Group given where one atom is needed |

### Export blockers by layer

| Layer | Codes that block export | of |
|-------|------------------------:|---:|
| `syntax` | 11 | 18 |
| `model` | 0 | 12 |
| `symbols` | 18 | 25 |
| `topology` | 3 | 8 |
| `official` | 1 | 10 |
| `lint` | 6 | 17 |
| `package` | 6 | 10 |

### Four codes that block export without the flag

The tables above are the catalogue. `Document.export_blockers()` adds four more findings at run time, and their `Code` entries all have `blocks_export = False`:

| Code | Catalogue severity | Added when |
|------|--------------------|------------|
| [`MOD004`](#mod004) | warning | a construct the interface cannot model contributes to the potential (`analysis().export_is_lossy()`) |
| [`OFF001`](#off001) | info | the official layer is `NOT_RUN` |
| [`OFF008`](#off008) | warning | the official layer is `STALE`, or its `source_rev` is not the current text revision |
| [`OFF009`](#off009) | warning | the topology file's digest has changed since the official run |

`OFF001` is the one every reader meets first. It is `info` severity and blocks nothing in the catalogue, but a run-ready export is refused until the installed Desmond parser has passed the current text, because a run-ready file is a claim about Desmond and only the engine can make it. This is why a file with no problems at all still will not export until you run `--official`.

### A blocked export never blocks Save as source

These are two different buttons and two different code paths, kept apart on purpose. `Document.save_source()` contains no reference to `export_blockers()`, `Report`, or any diagnostic: it encodes `self.source` and hands it to `safety.atomic_write()`. The only thing it can refuse is overwriting a file that changed on disk underneath you, and `allow_overwrite_changed=True` clears even that. `source_window.py` says the same in its module docstring: "**Save source** writes the bytes as they are. Always available."

Run from the project root, this is the whole story in four lines:

```python
from funnelforge.core.document import Document

doc = Document.open("tests/fixtures/undefined_symbol.pot")
doc.run_local_layers()
print("blockers:", [(b.code, b.severity) for b in doc.export_blockers()])
print("export   :", repr(doc.export_run_ready("/tmp/out.pot")[0]))
print("save     :", doc.save_source("/tmp/saved.pot"))
```

```
blockers: [('SEM001', 'error'), ('OFF001', 'info')]
export   : ''
save     : /tmp/saved.pot
```

The export returned an empty path and `/tmp/out.pot` was never created; the save wrote the file. So the two outcomes are:

| | What it writes | What can stop it |
|---|---|---|
| **Save source** | the current text, byte for byte, atomically | only an unexpected change to the file on disk, and only until you confirm |
| **Export run-ready** | the same bytes, but only once the gates pass | any of the 45 flagged codes, plus `MOD004`, `OFF001`, `OFF008`, `OFF009` |

This matters when the file will not parse. A `syntax` error sets both flags, so the panels go quiet and run-ready export is refused — but the text is never held hostage. You can always write your work to disk and keep editing, or hand the file to Desmond yourself and let the engine have the last word. As `SYN001` puts it: "The text is untouched on disk; fix the highlighted position, or keep working in the text editor and let the official validator have the last word."

---

## Keeping this page honest

Every number, title, flag and `explain` paragraph above comes from `CATALOG`. When a code is added, removed or reworded, this page is wrong until it is regenerated. The counts can be checked in one command:

```
$ python -c "from funnelforge.core.lang import diagnostics as D; \
print(len(D.CATALOG), [(l, len(D.codes_for_layer(l))) for l in D.LAYERS])"
100 [('syntax', 18), ('model', 12), ('symbols', 25), ('topology', 8), ('official', 10), ('lint', 17), ('package', 10)]
```

Running the module directly prints the same catalogue as plain text - `_print_catalog()`, with `[EX]` flag columns where this page has two `yes`/`no` columns - followed by `_demo()`, a worked example of a `Report` in each of its states:

```
$ python -m funnelforge.core.lang.diagnostics
==============================================================================
DIAGNOSTIC CATALOGUE - 100 codes over 7 independent layers
==============================================================================

--- SYNTAX (18 codes: 11 error, 5 warning, 2 info)
    Can this interface read the file losslessly?
    SYN001  error   [EX]  Unexpected token
              A token turned up where the grammar cannot use it, so the file
              ...
```

