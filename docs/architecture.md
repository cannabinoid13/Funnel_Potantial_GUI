# Architecture: reading and editing a `.pot` without breaking it

This document is for whoever has to extend the front end — the lexer, the CST,
the semantic model, or any of the checks built on them. It says what each layer
is allowed to do, what invariant holds at each boundary, which test enforces it,
and where to put a new function, node kind, diagnostic or lint check.

The code it describes:

| File | Lines | Role |
| --- | ---: | --- |
| `funnelforge/core/safety.py` | 1218 | bytes ↔ text (`Encoding`), path and archive safety, atomic writes |
| `funnelforge/core/lang/lexer.py` | 270 | text → lossless token stream |
| `funnelforge/core/lang/cst.py` | 644 | tokens → concrete syntax tree of spans |
| `funnelforge/core/lang/sema.py` | 1172 | tree → symbols, types, dependency graph, metadynamics model |
| `funnelforge/core/lang/registry.py` | 759 | which functions each Desmond release accepts |
| `funnelforge/core/lang/diagnostics.py` | 1596 | the 100-code catalogue and the seven-layer report |
| `funnelforge/core/lang/patch.py` | 150 | span edits and the "nothing else moved" proof |
| `funnelforge/core/document.py` | 647 | the document model that wires the above together |
| `funnelforge/potcheck.py` | 225 | headless CLI over the same core |
| `funnelforge/gui/source_window.py` | 845 | the desktop workbench over the same core |

The grammar being implemented is Desmond's own ANTLR3 source, which is on this
machine and readable:

```
/opt/schrodinger2025-3/internal/lib/python3.11/site-packages/schrodinger/
    application/desmond/enhanced_sampling/mexp.g
```

Every grammar claim below is checked against that file, by line number.

---

## 1. Why the regex importer had to go

The old reader is `funnelforge/core/potfile.py`. It is a bank of 14 regular
expressions (`potfile.py:72`–`96`) matched against comment-stripped text, and it
does not describe the M-expression language — it describes *one* file layout,
the one this project's own exporter writes. Six of the fourteen hard-code
identifier names — `origin`, `core_com`, `axis_raw`, `axis0`, `lig_com`, `hill`,
`r_allowed`, `v_total`, `e1`, `e2`, `p1`, `p2` — and two more hard-code this
project's own `# @ff-term` / `# @ff-axis` comment markers. Three examples:

```python
_RE_DIST = re.compile(rf"^\s*({_ID})\s*=\s*norm\s*\(\s*min_image\s*\(\s*"
                      rf"lig_com\s*-\s*({_ID})\s*\)\s*\)\s*;", re.M)      # potfile.py:85
_RE_HILL = re.compile(rf"^\s*hill\s*=\s*({_ID})\s*\*\s*exp\s*\(\s*({_ID})\s*/\s*"
                      rf"\(\s*-\s*({_ID})\s*\)\s*\)\s*;", re.M)           # potfile.py:87
_RE_TOTAL = re.compile(r"^\s*v_total\s*=\s*([^;]+);", re.M)               # potfile.py:96
```

`parse_pot` then requires the names `lig`, `site` and `core` outright
(`potfile.py:287`–`294`), and treats their absence as an error.

### The demonstration

`tests/fixtures/arbitrary_names.pot` is an ordinary two-accumulator
well-tempered potential — walls, a `series`, a `static`/`store` pair, two
`meta()` calls — written by someone who chose their own variable names. Put it
through the old path:

```
$ python -c "
from funnelforge.core.potfile import parse_pot
spec, rep = parse_pot(open('tests/fixtures/arbitrary_names.pot').read())
print('rep.ok        :', rep.ok)
print('summary       :', rep.summary())
print('recognised    :', rep.recognised)
print('errors        :'); [print('   ', e) for e in rep.errors]
print('warnings      :'); [print('   ', w) for w in rep.warnings]
"

rep.ok        : False
summary       : 4 constructs recognised, 22 statements not modelled, 7 warnings, 4 errors
recognised    : ['declare_meta', 'declare_output', '2 meta() call(s)', '4 print statement(s)']
errors        :
    Missing `lig = atomsel(...)` statement.
    Missing `site = atomsel(...)` statement.
    Missing `core = atomsel(...)` statement.
    Could not read the `origin = core_com + f*axis_raw` statement.
warnings      :
    Unused atomsel variable `grpA` was kept out of the model and will not be re-emitted.
    Unused atomsel variable `grpB` was kept out of the model and will not be re-emitted.
    `ktemp` not found; keeping the default 8.624466483.
    `h0` not found; keeping the default 0.1.
    `sigma_z` not found; keeping the default 0.5.
    No `v_total = ...` statement was found.
    No `hill = h0*exp(v_old/(-ktemp))` statement found; the export will add the standard well-tempered hill scaling.
```

`unknown_statements` holds all 22 real statements of the file — every atom
selection, both collective variables, the `series`, both walls, the
`static`/`store`, the `whim()` call, and the final `total;`. The reader saw the
whole potential and modelled none of it.

The damage is done at export. `emit_pot(spec)` does not patch the input; it
regenerates a file from the `FunnelSpec` defaults, so what the user gets back
is a *different potential*:

```
$ python -c "
from funnelforge.core.potfile import parse_pot, emit_pot
text = open('tests/fixtures/arbitrary_names.pot').read()
spec, rep = parse_pot(text)
out = emit_pot(spec)
print('input  bytes:', len(text), ' lines:', text.count(chr(10)))
print('output bytes:', len(out), ' lines:', out.count(chr(10)))
print('identical   :', out == text)
print(out)
"

input  bytes: 1382  lines: 41
output bytes: 1263  lines: 54
identical   : False

declare_meta(
  dimension = 2,
  cutoff = 9,
  ...
lig = atomsel("atom. ");

site = atomsel("atom. ");
core = atomsel("atom. ");
...
ktemp = 8.624466483;
h0 = 0.1;
sigma_z = 0.5;
sigma_rho = 0.5;
v_old = meta(0,array(0.0,0.0,0.0),array(z,rho));
hill = h0*exp(v_old/(-ktemp));
v_meta = meta(0,array(hill,sigma_z,sigma_rho),array(z,rho));

v_total = v_meta;


v_total;
```

Read that output against the input. The three atom selections are now
`atomsel("atom. ")` — **empty**. The two flat-bottom walls are gone. The
`series` radius-of-gyration collective variable is gone. The `static`/`store`
pair is gone. The `whim()` term is gone. The bias factor the user chose
(`kT_dt = 2.4943`, `0.239 * exp(...)`) has been replaced by this program's
defaults (`8.624466483`, `0.1`). The comments are rewritten. Nothing in that
file warns the user that their physics was substituted; `rep.errors` is
non-empty, but the emitter runs anyway and produces a plausible-looking
potential.

That is the failure mode the rest of this architecture exists to make
impossible: **a reader that only understands one dialect will silently rewrite
every other one.**

### The same file through the new path

```
$ python -m funnelforge.potcheck tests/fixtures/arbitrary_names.pot
                        # timestamp and revision columns trimmed to fit

syntax    ERROR    1 error
          note: 482 tokens, 28 statements
model     PASSED   2 info
          note: 28/28 statements modelled (100%), 0 unrecognised function(s)
symbols   PASSED   nothing to report
          note: 20 names, 19 reach the potential
topology  NOT_RUN  nothing to report                  never
official  NOT_RUN  nothing to report                  never
lint      PASSED   nothing to report
package   NOT_RUN  nothing to report                  never
  ERROR   SYN001       29:1  declarations belong at the top of the file: Desmond's
          grammar is 'header block', so this declaration after the first ordinary
          statement will be rejected by the engine
          evidence: static keeper(1);
  verdict: Desmond validation not performed. This interface: 1 error; not run:
           topology, package; the file does not parse here, which is a limit of
           this interface and not proof that Desmond would reject it.
  -- structure
     statements        28
     names             20 (19 reach the potential)
     selection grpA           unresolved (no structure supplied)
     selection grpB           unresolved (no structure supplied)
     declare_meta #0    dimension=2 name='kerseq'
     meta(acc 0) -> old_b            probe
     meta(acc 0) -> bias             bias, well-tempered (kDT=2.4943)
     potential         total;
       + bias  (via total) [meta]
       + wall_u  (via total) [wall]
       + wall_l  (via total) [wall]
       + extra  (via total)
```

All four contributions found, both `meta()` calls classified, the well-tempered
relation proved with the user's own `kDT = 2.4943`, and the file byte-for-byte
unchanged on disk. The one error is real and independently confirmed: the
fixture puts `static keeper(1);` on line 29, after the body, and `mexp.g:52` is
`prog : header block EOF`. The engine's own message, recorded in
`tests/fixtures/manifest.json`, is `line 29:0 no viable alternative at input
'static'` — the same defect at the same place.

---

## 2. The layering

```
bytes
  │  safety.Encoding.decode          safety.py:211
  ▼
text (LF-normalised, str)
  │  lexer.tokenize                  lexer.py:124
  ▼
tokens (every character claimed)
  │  cst._Parser.parse               cst.py:219
  ▼
CST (spans over the text; OPAQUE where the grammar ran out)
  │  sema.analyse                    sema.py:329
  ▼
Analysis (symbols, types, graph, metas, contributions) + Problem list
  │  Document._to_diag / diag.make   document.py:390, diagnostics.py:1017
  ▼
Report (seven layers, each with its own state and source revision)
```

Edits run backwards through the same addresses: a `patch.Edit` is a character
range on the text, produced from a CST node's `start`/`end`, applied by
`patch.apply` (`patch.py:68`), and the text is re-encoded by
`Encoding.encode` (`safety.py:226`).

### What each layer may and may not do

| Layer | Must | Must not |
| --- | --- | --- |
| `Encoding` | reproduce the exact bytes it was given; report BOM, newline style, final newline | normalise, add or strip a final newline (`safety.py:226`–`243`) |
| `lexer` | assign every character to exactly one token; record problems | reject a file; discard, merge or reorder text |
| `cst` | build spans; recover into `OPAQUE`; record problems | copy or rewrite text; drop a token; raise on bad input |
| `sema` | derive a read-only view addressed by offsets | mutate the tree or the source; infer from identifier names |
| `registry` | say what a release's table contains | decide validity — `known() == False` means *unrecognised*, not *wrong* (`registry.py:525`) |
| `diagnostics` | hold the code catalogue and per-layer state | let anything but a real engine run set the `official` layer (`diagnostics.py:1130`) |
| `patch` | change only the given ranges | reformat, re-indent, or regenerate |

### The invariants, and the test for each

Every one of these lives in `tests/test_lang.py`; all 33 tests pass on this
tree.

| Invariant | Where it is stated | Test |
| --- | --- | --- |
| `"".join(t.text for t in tokenize(s)) == s` | `lexer.py:19`–`21` | `test_the_lexer_loses_nothing` (58) |
| `enc.encode(enc.decode(raw)) == raw`, for BOM/CRLF/mixed/no-final-newline | `safety.py:269`–`293` | `test_line_endings_and_bom_are_preserved_exactly` (67) |
| No non-trivia token is claimed by zero statements | `cst.py:628` `coverage_gaps` | `test_every_fixture_parses_and_nothing_is_orphaned` (83) |
| Unreadable syntax becomes one `OPAQUE` span, the rest still parses | `cst.py:281` `_opaque_from` | `test_unknown_syntax_survives_as_an_opaque_span` (93) |
| An unknown *function* is ordinary syntax, never opaque | `sema.py:1111` `_check_functions` | `test_unknown_functions_are_preserved_not_rejected` (112) |
| `-x^2` is `-(x^2)`; `^` is right-associative | `cst.py:411`, `cst.py:422` | `test_operator_precedence_matches_the_grammar` (128) |
| `meta` inside a comment or string is not a call | lexer tokenises both as trivia/STRING | `test_a_comment_or_string_never_creates_a_meta_call` (139) |
| No identifier name is required anywhere | whole of `sema.py` | `test_no_fixed_names_are_required` (153) |
| Every `meta()` is found at any nesting depth | `sema.py:648` `_collect_metas` | `test_nested_and_aliased_meta_calls_are_all_found` (166) |
| Well-tempering is proved from data flow, not text | `sema.py:875` `_well_tempered` | `test_probe_and_bias_are_distinguished_and_wt_is_proved` (182) |
| Plain metadynamics is not upgraded to well-tempered | same | `test_plain_metad_is_not_reported_as_well_tempered` (195) |
| Accumulators are judged one at a time | `sema.py:898` index match | `test_multiple_accumulators_are_judged_separately` (205) |
| A definition feeding only `print` is not dead | `sema.py:732`–`740` | `test_a_selection_is_not_unused_when_it_only_feeds_a_print` (226) |
| A structured edit moves only its own span | `patch.py:93` `unchanged_outside` | `test_a_structured_edit_touches_only_its_own_span` (282) |
| Overlapping edits raise instead of picking a winner | `patch.py:71`–`74` | `test_overlapping_edits_are_refused` (299) |
| Open-then-save is byte-identical for the whole corpus | `document.py:555` | `test_open_and_save_is_byte_for_byte_identical` (310) |
| The interface never says "Desmond-valid" on its own | `diagnostics.py:1230` `verdict` | `test_the_interface_never_claims_desmond_valid_on_its_own` (343) |
| A lossy export writes nothing at all | `document.py:623` | `test_a_lossy_export_is_blocked_before_anything_is_written` (356) |
| A result never outlives the text it describes | `diagnostics.py:1161` `stale` | `test_results_go_stale_when_the_text_changes` (377) |
| 400 mutated inputs: no crash, no hang, no text loss | — | `test_fuzzing_never_crashes_hangs_or_loses_text` (450) |
| 5000-deep nesting is bounded, not fatal | `cst.py:591` `max_depth` | `test_deeply_nested_input_is_bounded_not_fatal` (481) |

The corpus is the 30 `.pot` files in `tests/fixtures/`, plus one real job when
it is present — `$FUNNELFORGE_TEST_JOB`, defaulting to the path at
`test_lang.py:38`–`42`, with the tests that need it returning early when it is
not there. `tests/fixtures/manifest.json`
records each fixture's bytes, sha256, encoding, newline style, and what the real
2025-3 `enhsamp.parseStr` said about it; `tests/fixtures/differential.json` is
the condensed interface-vs-engine agreement table.

---

## 3. The lexer

`lexer.py` is a hand-written scanner with one governing property, stated in its
own docstring at line 19:

```python
"".join(t.text for t in tokenize(src)) == src
```

### Trivia are tokens, not gaps

`WS`, `NEWLINE` and `COMMENT` are token kinds (`lexer.py:38`–`49`), so nothing
sits between tokens. That is the entire reason offsets are trustworthy
downstream:

```
$ tokenize('a = 1.0e-3;  # note\nb;\n')

  ident    'a'            0-1   1:1
  ws       ' '            1-2   1:2
  op       '='            2-3   1:3
  ws       ' '            3-4   1:4
  number   '1.0e-3'       4-10  1:5
  op       ';'           10-11  1:11
  ws       '  '          11-13  1:12
  comment  '# note'      13-19  1:14
  newline  '\n'          19-20  1:20
  ident    'b'           20-21  2:1
  op       ';'           21-22  2:2
  newline  '\n'          22-23  2:3
  eof      ''            23-23  3:1
round trip : True
```

Note the comment token stops *before* the newline (`lexer.py:176`–`182`). The
line ending stays its own token, so a CRLF file survives as one `NEWLINE` token
holding `"\r\n"` (`lexer.py:155`–`159`) rather than being split and rejoined.

### The lexer never refuses a file

`LexProblem` (`lexer.py:105`) is raised for exactly one condition: exceeding
`max_tokens` (`lexer.py:148`–`151`), which is a resource guard. An unterminated
string or a stray character produces a token *and* a problem:

```
'x = .5;'   -> [('ident','x'),('op','='),('unknown','.'),('number','5'),('op',';')]
               SYN003  character '.' is not part of the M-expression language
's = "abc'  -> [('ident','s'),('op','='),('string','"abc')]
               SYN002  unterminated string literal
```

### The two grammar subtleties

`mexp.g:106` is:

```
LIT 	:	DIGIT+ (  '.' DIGIT* ) ? (('e'|'E') ('+' | '-') ? DIGIT+)? ;
```

A literal **must start with a digit**. `.5` is not a number in this language —
it is a character that is not in the language at all, followed by `5`. `1.` *is*
a number. And an `e` not followed by digits is not part of the literal: it
starts an identifier. All three fall out of `lexer.py:199`–`220`:

```
'x = 1.;'   -> [..., ('number','1.'), ('op',';')]
'x = 1e;'   -> [..., ('number','1'), ('ident','e'), ('op',';')]
'x = 1e5;'  -> [..., ('number','1e5'), ('op',';')]
```

`mexp.g:117` is:

```
STRING	:	'"' (~'"')* '"' ;  // FIXME -- strings with escaped characters are not handled
```

The vendor's own comment says it. There is **no escape mechanism**: a string
runs to the very next `"`, and a `"` inside one is impossible. Writing the
familiar backslash escape does not do what a C or Python programmer expects:

```
$ tokenize(r'print("say \"hi\"", x);')

[('ident','print'), ('op','('), ('string','"say \\"'), ('ident','hi'),
 ('unknown','\\'), ('string','""'), ('op',','), ('ident','x'),
 ('op',')'), ('op',';')]
```

Two strings and a stray backslash, not one string. Because the raw span is
preserved (`lexer.py:184`–`197`), the file still round-trips exactly and the
parser reports the mess rather than the lexer inventing an escape rule Desmond
does not have.

### Reserved words

`KEYWORDS` (`lexer.py:53`) holds the words the lexer classifies as `KEYWORD`.
`RESERVED` (`lexer.py:64`) is different and worth understanding: ANTLR3 turns
every quoted literal in `mexp.g` into a lexer token that outranks `IDENT`, so
the six declaration attribute words plus `inf` are reserved *everywhere*, not
only inside a declaration. They stay `IDENT` in the token stream — the parser
still needs them as declaration keys — and `cst.py:309`–`316` reports the misuse
when one is used as a variable.

---

## 4. The CST

### Spans, not copies

`Node` (`cst.py:81`) carries `start`/`end` (character offsets) and `ti`/`tj`
(half-open token indices). It carries no text. `span_text(src)` slices the
original string on demand (`cst.py:109`). Nothing in the tree can drift from the
file, because nothing in the tree is a copy of the file.

`ti`/`tj` exist for editing: they are how a caller finds the exact tokens a node
covers when it needs to widen an edit to include surrounding trivia.

### Node kinds

Statement level (`STATEMENT_KINDS`, `cst.py:76`): `DECL_META`, `DECL_OUTPUT`,
`STATIC`, `BIND`, `EXPR_STMT`, `OPAQUE`.

Expression level: `NUM`, `STR`, `NAME`, `CALL`, `INDEX`, `UNARY`, `BINARY`,
`IFELSE`, `SERIES`, `ITER`, `BLOCK`, `PAREN`, plus `KWARG` for a declaration
term and `STATIC_VAR` for one `name(size)` entry.

`PAREN` is a node rather than a discarded pair of brackets. That is deliberate:
a parenthesis is a character in the file, and dropping it would make
`replace_node` on the inner expression eat the brackets.

### Recovery into `OPAQUE`

The parser is recursive descent over the non-trivia tokens
(`self.code`, `cst.py:168`). When a production fails it raises `_Recover`
(`cst.py:583`); `_statement` catches it, records a `Problem`, and calls
`_opaque_from` (`cst.py:281`), which rewinds to the start of the statement and
consumes to the next bracket-balanced `;`. The result is one node with the exact
span and nothing else lost:

```
$ parse('declare_output(...);\ngood = 1.0;\nmystery @@ from the future;\n'
        'total = good + mystery;\ntotal;\n')

   decl_output [  0: 56) 'declare_output(name = "o", first = 0.0, interval = 1.0);'
   bind        [ 57: 68) 'good = 1.0;'
   opaque      [ 69: 96) 'mystery @@ from the future;'
   bind        [ 97:120) 'total = good + mystery;'
   expr_stmt   [121:127) 'total;'

problems     : SYN003 (x2, the '@' characters), SYN001 "expected ';' after this statement"
coverage_gaps: []
```

Depth is bounded too. `parse(max_depth=...)` raises the CPython recursion limit
for the duration and restores it (`cst.py:616`–`623`); overrunning `max_depth`
raises `RecursionError`, which `_statement` converts to `SYN010` and an opaque
span (`cst.py:264`–`268`). 5000 nested parentheses give one `SYN010`, one opaque
statement, and no gaps.

### `coverage_gaps()` — the machine-checkable "nothing was dropped"

`coverage_gaps` (`cst.py:628`) marks every token index inside any top-level
statement's `[ti, tj)` range, then returns every non-trivia, non-EOF token that
no statement claimed. It must always return `[]`. It is not a fallback path or a
repair step; it is the assertion that turns "the parser is careful" into a
property a test can check, and
`test_every_fixture_parses_and_nothing_is_orphaned` runs it over the whole
corpus.

If you add a statement form and forget to give its node the right `ti`/`tj`,
this is the test that fails.

### Precedence, exactly as the grammar has it

`mexp.g:83`–`90`:

```
expr	:	factor ( (ADDOP | SUBTROP)^ factor )* ;
factor	:	signedExpComp  ( (MULTOP | DIVOP )^ signedExpComp )* ;

// does not handle multiple negation
signedExpComp  : (ADDOP^ | SUBTROP^)? expComp;
expComp :	comp (EXP^ signedExpComp) ? ;

comp	:	a=atom (BOPEN b+=expr BCLOSE) *  -> ^(ELEM $a $b*);
```

Read `signedExpComp` and `expComp` together. The optional sign wraps a whole
`expComp`, and `expComp` is where `^` lives. Therefore **unary sign binds looser
than `^`**, and `-x^2` is `-(x^2)`, not `(-x)^2`. This is the opposite of what
several languages do and the reason the rule is spelled out here.

The `^` right operand is a `signedExpComp`, not a `comp`, so `^` is
right-associative and its exponent may itself be signed — `2^-3` parses.
`_signed` (`cst.py:411`) and `_exp_comp` (`cst.py:422`) implement exactly this
pair.

```
$ parse("y = -x^2;")
bind:y [0:9) 'y = -x^2;'
  unary:- [4:8) '-x^2'
    binary:^ [5:8) 'x^2'
      name:x [5:6) 'x'
      num:2 [7:8) '2'

$ parse("z = 2.0^-3.0^2.0;")
bind:z [0:17) 'z = 2.0^-3.0^2.0;'
  binary:^ [4:16) '2.0^-3.0^2.0'
    num:2.0 [4:7) '2.0'
    unary:- [8:16) '-3.0^2.0'
      binary:^ [9:16) '3.0^2.0'
        num:3.0 [9:12) '3.0'
        num:2.0 [13:16) '2.0'
```

`z` is `2.0 ^ (-(3.0 ^ 2.0))`, which is what Desmond computes.

The grammar's own comment on line 86, "does not handle multiple negation", is
also honoured. `--x` is a syntax error in Desmond, and this parser agrees at the
same position:

| | message |
| --- | --- |
| `enhsamp.parseStr` (2025-3, real run) | `line 2:5 no viable alternative at input '-'` |
| `cst.parse` | `SYN001 expected a value, found '-'`, statement preserved as `OPAQUE` |

### Grammar rules enforced before the engine sees the file

Two of Desmond's rules produce famously opaque engine messages, so the parser
reports them itself:

* `mexp.g:52` is `prog : header block EOF`. A `declare_meta`, `declare_output`
  or `static` after the first ordinary statement is rejected by the engine with
  `no viable alternative at input 'static'`. `cst.py:231`–`240` reports it as
  `SYN001` with the reason spelled out.
* `mexp.g:123` defines a comment as running *up to a newline*. A comment as the
  last thing in a file, with no trailing newline, makes the engine say
  `no viable alternative at character '<EOF>'`. `cst.py:605`–`615` reports
  `SYN006`.

Both are covered by
`test_the_engines_grammar_rules_are_enforced_before_the_engine` (269).

---

## 5. The semantic layer

`sema.analyse` (`sema.py:329`) runs ten passes in a fixed order and returns an
`Analysis` (`sema.py:181`). It never writes to the tree or the source.

```python
_collect_declarations(a)     # sema.py:349
_collect_opaque_defs(a)      # sema.py:364
_collect_symbols(a)          # sema.py:391
_infer_types(a, topology)    # sema.py:491
_collect_metas(a)            # sema.py:648
_find_energy(a)              # sema.py:690
_reachability(a, ...)        # sema.py:703
_classify_metas(a)           # sema.py:842
_check_functions(a)          # sema.py:1111
_check_declarations(a)       # sema.py:1130
```

### Scopes

`_walk_statements` (`sema.py:380`) yields `(statement, scope-id)` for the
program and for every nested `BLOCK`, with ids like `program/block@1234`.
Duplicate assignment is checked per scope; only **program-scope** names go into
`Analysis.symbols` and the dependency graph (`sema.py:415`–`419`), because a
name bound inside a `{ ... }` block cannot be referred to from outside it.

`series` iterators do not create an entry in that walk at all
(`sema.py:387`–`388`). They are handled entirely by `_free_names`
(`sema.py:299`–`309`), which blocks an iterator's name inside the series body.
The consequence is correct in both directions:

```
$ 'v = { t = 1.0; t = 2.0; t };  v;'
   program symbols: ['v']
   problems       : SEM002  `t` is assigned again ... (first at line 1)

$ 'a = series (k = 0:3.0) k * 2.0;  b = k;  b;'
   symbols  : ['a', 'b']
   problems : SEM001  `k` is used but never defined
```

The block-local `t` is checked for duplication but never pollutes the global
table; the iterator `k` is legal inside the series and undefined outside it.

Undefined names are found by walking only top-level statements
(`sema.py:443`–`458`), because `_free_names` has already accounted for every
nested scope. Declaration statements are skipped in that walk, which is why
`interval = inf` in a `declare_output` produces no false `SEM001`.

### The `Type` model

```python
@dataclass(frozen=True)        # sema.py:44
class Type:
    cat: str = UNKNOWN         # selection | number | string | unknown
    dim: int | None = None     # array length; None means "not determined"
    note: str = ""
```

There is no scalar/vector distinction because the language has none.
`registry.py:22`–`38` records the convention from the vendor's own source:
every value is a fixed-length array of doubles, and the whole type of a value
*is* that length. A scalar is length 1; a 3-vector is length 3. `Type.__str__`
(`sema.py:58`) renders `dim == 1` as `scalar` and `dim == 3` as `vector` purely
as a courtesy to the reader; nothing branches on those words.

`dim is None` means the analysis could not work the length out. That is reported
as "unknown", never as an error (`sema.py:50`–`51`).

`SELECTION` is the one category outside the numeric world. `atomsel(...)` yields
`Type(SELECTION, n)` where `n` is the resolved atom count when a topology has
been supplied and `None` otherwise (`sema.py:565`–`571`), so an unresolved
selection is visibly *unresolved* rather than assumed.

Inference rules of note (`sema.py:503`–`599`):

* `BINARY`: equal lengths give that length; a length-1 operand broadcasts to the
  other's length; mismatched non-1 lengths give `dim=None` with the note
  `"operands of different lengths"`. This mirrors the `binary_thread` rule in
  `registry.py:52`–`57`.
* `array(...)` sums its arguments' lengths (`sema.py:572`–`579`), and nested
  `array` calls are spliced by `_array_items` (`sema.py:257`) because `array`
  concatenates rather than nests.
* A registry `thread`/`binary_thread` signature returns the first argument's
  type. A negative `ret` code is a wildcard resolved by finding the argument
  with the matching code (`sema.py:592`–`596`).
* A name not in the registry gives `Type(UNKNOWN, None, "`x` is not in the
  registry")` — a note, not a diagnostic.

Worked example:

```
  g         selection[?]   dim=None   g = atomsel("atom. 1-10")
  c         vector         dim=3      c = center_of_mass(g)
  d         vector         dim=3      d = min_image(c - c)
  n         scalar         dim=1      n = norm(d)
  arr       array[4]       dim=4      arr = array(n, n, n, n)
  s         string         dim=1      s = "label"
  q         unknown        dim=None   q = quantum_cv(n)     note: not in the registry
  w         array[4]       dim=4      w = arr * 2.0         (length-1 broadcast)
  first_el  scalar         dim=1      first_el = arr[0]
```

### The dependency graph

Built in `_collect_symbols` (`sema.py:421`–`435`):

* `Symbol.deps` — names the right-hand side reads, via `_free_names`
* `Analysis.graph` — `name -> deps`, the forward edges
* `Analysis.users` — the reverse edges
* `Symbol.uses` — every `NAME` node in the file that resolves to this symbol,
  which is what "go to references" and rename would use

Cycles are found by a three-colour DFS (`_detect_cycles`, `sema.py:463`) and
reported as `SEM003` with the cycle path in the message.

### Reachability: two roots, not one

`_reachability` (`sema.py:703`) computes two closures over `graph`.

**From the potential.** `_find_energy` (`sema.py:690`) takes the file's *last*
expression statement as the potential — not a variable called `v_total`. The
free names of that expression seed `Analysis.reachable`, and every symbol in the
closure gets `reaches_energy = True`.

**From side effects.** Every call whose registry entry has `side_effect=True`
(`print`, `store`, `meta`, `declare_meta`, `declare_output` —
`registry.py:535`) contributes its arguments' free names as a second root
(`sema.py:732`–`740`). Those symbols get `reaches_side_effect = True`.

Keeping them apart is what stops a dead-code pass from deleting a diagnostic:

```
  g   energy=False side_effect=True   deps=[]     users=['c']
  c   energy=False side_effect=True   deps=['g']  users=['d']
  d   energy=False side_effect=True   deps=['c']  users=[]
  e   energy=True  side_effect=False  deps=[]     users=[]
```

`d` is printed and never enters the energy. It is not unused, and
`test_a_selection_is_not_unused_when_it_only_feeds_a_print` (226) holds that
line.

### Contributions, expanded through single-name aliases

Almost every real potential ends with `v_total;`. Listing that as "the one
contribution" would be true and useless. `_expand_contributions`
(`sema.py:768`) flattens the final expression into additive terms
(`_flatten_sum`, `sema.py:317`), and when a term is *just a name* whose
definition is itself a sum, it substitutes that sum's terms and records the
chain it came through. Expansion stops at anything that is not a plain alias, so
a genuine single term is reported as itself. Depth is capped at 8.

```
arbitrary_names.pot ends with:  total  = bias + wall_u + wall_l + extra;  total;

   bias    (via total)   [meta]
   wall_u  (via total)   [wall]
   wall_l  (via total)   [wall]
   extra   (via total)
```

`Contribution.kinds` (`sema.py:750`–`755`) is a coarse tag set: `meta` if the
term's definition contains a `meta()` call anywhere, `wall` if it contains an
`IFELSE`. It is a label for the UI, not a classification the rest of the code
depends on.

### Opaque, and when it becomes a refusal

`_collect_opaque_defs` (`sema.py:364`) looks at each `OPAQUE` node's first two
non-trivia tokens. If they are `IDENT` `=`, the name is recorded in
`Analysis.opaque_defs`: it is not a real symbol, but it exists as far as the
rest of the file is concerned, so it is not reported as undefined *and* anything
reading it is known to stand on unmodelled ground.

```
'mystery @@ from the future;'     opaque_defs: []          -> SEM001 `mystery` undefined
'mystery = @@ from the future;'   opaque_defs: ['mystery'] -> no SEM001
```

Either way, `_touches_opaque` (`sema.py:795`) marks the contribution opaque —
in the first case because `mystery` has no symbol at all (`sema.py:810`–`815`),
in the second because it is in `opaque_defs`. `export_is_lossy`
(`sema.py:222`) is then true, `Document.export_blockers` adds `MOD004`, and
`export_run_ready` writes nothing (`document.py:631`–`632`). `save_source`
remains available and lossless; that separation is the whole point.

---

## 6. MetaD and the well-tempered relation

This is the part where the design earns its keep, so it is worked through in
full.

### Finding every `meta()` call

`_collect_metas` (`sema.py:648`) walks the entire program tree — not the
top-level statements, not the symbol values, the whole tree — and takes every
`CALL` node named `meta`. Depth is irrelevant, and the owner (the enclosing
program-scope binding, if any) is recovered separately by scanning each symbol's
value for the same node identity (`sema.py:650`–`654`).

`tests/fixtures/nested_meta.pot` hides six calls inside a call argument, an `if`
`then`, an `if` `else`, a `series` body and a `{ }` block, and mentions
`meta(` seven more times inside comments to catch a text scanner:

```
   acc=0 role=bias        depth=5  owner=buried_in_call
   acc=0 role=bias        depth=3  owner=buried_in_then
   acc=0 role=bias        depth=4  owner=buried_in_else
   acc=0 role=bias        depth=3  owner=buried_in_series
   acc=0 role=bias        depth=5  owner=buried_in_block
   acc=0 role=diagnostic  depth=3  owner=-
```

Six calls, six found, the comment mentions ignored, and the one that is only
ever printed classified as `DIAGNOSTIC`.

### Resolving arrays through aliases

`meta(id, hills, cvs)` is almost never written with both arrays inline. A file
that binds them first is completely ordinary:

```
cv_pair      = array(cv_axial, cv_radial);
probe_widths = array(0.0, 0.0, 0.0);
deposit      = array(h_now, width_axial, width_radial);
v_bias       = meta(0, deposit, cv_pair);
```

`resolve_array` (`sema.py:618`) follows a `NAME` through the symbol table,
splices nested `array` calls, and recurses up to depth 32:

```
$ resolve_array(an, bias.hills)  ->  ['h_now', 'width_axial', 'width_radial']
$ resolve_array(an, bias.cvs)    ->  ['cv_axial', 'cv_radial']
```

Without this, `n_hills` and `n_cvs` would be `None`, `MTD004`/`MTD005` could not
check the declared dimension, and — the important one — the well-tempered test
would never find the hill height, so every aliased well-tempered file would be
reported as plain metadynamics.

### Roles

`_classify_metas` (`sema.py:842`) assigns each call one of five roles:

| Role | Condition | Where |
| --- | --- | --- |
| `PROBE` | every element of the hill array folds to `0.0` | `sema.py:666`–`672`, `855` |
| `BIAS` | the call node is in the potential's dependency closure | `sema.py:857` |
| `DIAGNOSTIC` | the node is in a `print` argument's closure | `sema.py:859` |
| `INTERMEDIATE` | bound to a name that something else reads | `sema.py:861` |
| `UNDETERMINED` | none of the above | `sema.py:865` |

A zero-height `meta()` deposits nothing and returns the bias accumulated so far
— it is a *read*, not a deposit. Distinguishing it from a real deposit is what
makes the next step possible.

### `metad_2d_wt.pot`, the worked example

The fixture is deliberately hostile to pattern matching. Its own header says so:

> The well-tempered scaling is deliberately smeared over eight intermediate
> bindings. No single line contains the "h0 \* exp(-V/kT)" idiom, the minus sign
> lives in its own binding, and the probe call is separated from the outer
> deposit by four unrelated statements.

The relevant chain, in file order (lines 39–62):

```
boltzmann     = 0.0019872041;
sim_temp      = 310.0;
thermal       = boltzmann * sim_temp;

bias_factor   = 15.0;
excess_factor = bias_factor - 1.0;
delta_t       = excess_factor * thermal;
neg_delta_t   = 0.0 - delta_t;

cv_pair       = array(cv_axial, cv_radial);
probe_widths  = array(0.0, 0.0, 0.0);

v_accumulated = meta(0, probe_widths, cv_pair);      <- the probe

tempering_arg = v_accumulated / neg_delta_t;
tempering     = exp(tempering_arg);
h_initial     = 0.10;
h_now         = h_initial * tempering;

deposit       = array(h_now, width_axial, width_radial);

v_bias        = meta(0, deposit, cv_pair);           <- the deposit
```

There is no line matching `h0 * exp(-V/kT)`. There is no literal minus sign in
front of anything: the negation is `0.0 - delta_t`. The probe and the deposit
are eleven lines apart.

### How `_well_tempered` proves it

`_well_tempered` (`sema.py:875`) is given the deposit call and its hill height
(`items[0]` from the resolved array — here `h_now`). It then asks four questions
about the *data flow*, in order.

**1. Is there an `exp()` anywhere in the height's closure?**

`_expr_closure` (`sema.py:826`) collects every node reachable from an
expression, following free names through the symbol table. From `h_now` that
reaches `h_initial`, `tempering`, `exp(tempering_arg)`, `tempering_arg`,
`v_accumulated`, `neg_delta_t`, `delta_t`, and so on. The `exp` call is found
even though it is two assignments away (`sema.py:884`–`888`).

**2. Does that `exp`'s argument read this accumulator's bias back?**

The closure of the argument (`tempering_arg`) is searched for a `meta()` call
that is itself a known `PROBE` on the same accumulator index
(`sema.py:893`–`901`). It finds `v_accumulated = meta(0, probe_widths,
cv_pair)`, whose `zero_height` is `True` because `resolve_array` gave
`[0.0, 0.0, 0.0]` and every element folded to zero.

**3. Does the bias enter the exponent with a negative coefficient?**

This is `coefficient_sign(a, arg, probe.node)` (`sema.py:1016`). It walks the
expression structurally, tracking the sign of the coefficient multiplying a
specific CST node, and resolves names through the symbol table as it goes:

* `NAME` → recurse into its definition (`sema.py:1036`–`1040`)
* `UNARY -` → flip (`sema.py:1041`–`1045`)
* `+`/`-` → recurse into whichever side contains the target; a right operand of
  `-` flips (`sema.py:1052`–`1059`)
* `*`/`/` → recurse into the side containing the target and multiply by the sign
  of `const_value` of the other side (`sema.py:1061`–`1070`)
* target in a denominator → `None`, because that is not a linear coefficient
  (`sema.py:1062`–`1063`)
* target on both sides of an operator → `None` (`sema.py:1050`–`1051`)

`const_value` (`sema.py:952`) is the constant folder that makes this work. It
resolves names through the symbol table and folds `+ - * / ^`, returning `None`
the moment it meets anything runtime-dependent. On this fixture:

```
const_value(thermal)     =  0.6160332709999999
const_value(delta_t)     =  8.624465793999999
const_value(neg_delta_t) = -8.624465793999999
```

So `coefficient_sign(tempering_arg, probe_node)`:
`tempering_arg` is a `NAME` → its value `v_accumulated / neg_delta_t` is a
`BINARY /`; the target is on the left; the other side folds to a *negative*
number; the sign of the left side alone is `+1`; the product is **`-1`**.

The minus sign was never written as a minus sign. It was recovered by folding
`0.0 - delta_t`.

**4. What are `kDT` and `h0`?**

`_ktemp_of` (`sema.py:929`) reads the magnitude of the divisor or reciprocal
multiplier once it knows which side of the `*`/`/` holds the bias:
`abs(const_value(neg_delta_t)) = 8.624465794`.

`_leading_factor` (`sema.py:1090`) reads `h0` from the `h0 * exp(...)` product,
resolving one level of name indirection: `h_now = h_initial * tempering`, and
`h_initial = 0.10`.

The result:

```
$ sema.analyse(cst.parse(open('tests/fixtures/metad_2d_wt.pot').read()))

meta idx=0 role=probe zero=True  n_hills=3 n_cvs=2 owner=v_accumulated depth=2
meta idx=0 role=bias  zero=False n_hills=3 n_cvs=2 owner=v_bias        depth=2

confirmed : True
kDT       : 8.624465793999999
h0        : 0.1
evidence  : the hill height reads accumulator 0 back through exp() with a
            negative coefficient; kDT = 8.62447 kcal/mol; h0 = 0.1 kcal/mol
```

Cross-check against the physics the file describes: γ = 15, T = 310 K,
kB = 0.0019872041 kcal/mol/K, so kΔT = (γ−1)·kB·T = 14 × 0.0019872041 × 310 =
`8.624465793999999`. Exact match, and
`test_probe_and_bias_are_distinguished_and_wt_is_proved` (182) asserts it to
within 1e-9.

### Why this beats pattern matching

Every one of these spellings is confirmed, with the same `kDT` and `h0`:

| Written as | confirmed | kDT | h0 |
| --- | --- | --- | --- |
| `h = 0.1 * exp(v_old / (-kdt));` | yes | 8.624466 | 0.1 |
| `h = 0.1 * exp(-v_old / kdt);` | yes | 8.624466 | 0.1 |
| `h = 0.1 * exp(v_old * (-1.0 / kdt));` | yes | 8.624466 | 0.1 |
| `negk = 0.0 - kdt;`<br>`h = 0.1 * exp(v_old / negk);` | yes | 8.624466 | 0.1 |
| `inv = -1.0 / kdt;`<br>`arg = v_old * inv;`<br>`h = 0.1 * exp(arg);` | yes | 8.624466 | 0.1 |

And the negatives are negative for the right stated reason:

| Written as | confirmed | evidence |
| --- | --- | --- |
| `h = 0.1 * exp(v_old / kdt);` | no | "the accumulated bias enters the exponent with a positive coefficient, which is not the well-tempered relation" |
| `h = 0.1 * exp(kdt / v_old);` | no | "the accumulated bias reaches the exponent, but the sign of its coefficient could not be established from the data flow" |
| `metad_1d.pot` (plain MetaD) | no | "no exp() in the hill height's data flow" |
| `multi_accumulator.pot`, acc 1 | no | "no exp() in the hill height's data flow" |

`multi_accumulator.pot` is the case that matters for correctness: the same file
has accumulator 0 well-tempered and accumulator 1 not, and the two are judged
separately because the probe match requires `p.index == mc.index`
(`sema.py:898`).

**Caveat for maintainers:** `WellTempered.ktemp` is computed whether or not
`confirmed` is true: `sema.py:906` computes it unconditionally, whatever the
sign turned out to be. In the
`exp(kdt / v_old)` row above it reports 8.624466, which is meaningless there.
Read `ktemp` only when `confirmed` is true — `document.py:334` and
`potcheck.py:82` both do.

---

## 7. Patch-based editing

A structured edit in this program is not a re-serialisation. There is no
pretty-printer, no `to_source()`, no code path that turns a CST back into text.
An edit is a character range and a replacement string:

```python
@dataclass(frozen=True)
class Edit:                # patch.py:32
    start: int
    end: int
    text: str
    why: str = ""
```

`patch.apply` (`patch.py:68`) sorts the edits, refuses any overlap with
`OverlappingEdits` (`patch.py:71`–`74`), refuses an edit running past the end,
and then copies the source in one pass: `src[pos:e.start]`, then `e.text`, then
on to the next. The text between edits is *the same string object's characters*,
copied verbatim.

That is the mechanism. Indentation, alignment, comment placement, numeric
spelling (`1.0e-3` stays `1.0e-3`, `9` does not become `9.0`), statement order,
blank lines and line endings survive **by construction**, because no code
anywhere is in a position to change them. There is no formatter to disable.

`apply` returns a `PatchResult` whose `applied` tuple records, per edit, where
the replacement text landed in the new document (`patch.py:52`–`58`), so a
caller can re-anchor a cursor or a selection.

`unchanged_outside(old, new, edits)` (`patch.py:93`) is the proof obligation. It
re-applies the edits to `old` and compares to `new`; anything that rewrote text
it was not asked to touch fails. `Document.apply_edits` (`document.py:145`)
calls it on every structured edit and raises rather than committing:

```python
if not patch.unchanged_outside(self.source, res.text, edits):
    raise RuntimeError(
        "the edit changed text outside its own spans; refusing")
```

A real edit on the fixture:

```
$ node = an.symbols['h_initial'].value          # <num:0.10 1572-1576>
$ doc.apply_edits([patch.replace_node(node, '0.05', 'lower the initial hill height')])

delta             : 0
applied           : Applied(edit=Edit(1572, 1576, '0.05', ...), new_start=1572, new_end=1576)
unchanged_outside : True

--- before
+++ after
@@ -54,3 +54,3 @@
 tempering = exp(tempering_arg);
-h_initial = 0.10;
+h_initial = 0.05;
 h_now = h_initial * tempering;

lines before/after: 69 69
```

One line differs. `test_a_structured_edit_touches_only_its_own_span` (282) does
this over every file in the corpus, then undoes it and checks the source is
identical again.

Helpers that build edits from CST nodes: `replace_node` (`patch.py:106`),
`insert_before`/`insert_after` (`111`, `115`), `delete_statement` (`119`, which
widens the span to swallow the line's leading whitespace and trailing newline so
a deletion does not leave a stray indent), `line_span` (`137`), and
`diff_summary` (`144`, used by the GUI's confirm-before-save dialog at
`source_window.py:513`). Of these, only `replace_node` and `diff_summary`
currently have callers outside `patch.py`; the other four are available API with
no user yet.

---

## 8. How to extend it

### Add a Desmond function

**File: `funnelforge/core/lang/registry.py`.**

1. Pick the right table by the shape of the rule:
   * one-argument element-wise → `_THREAD` (`registry.py:251`), a
     `(name, doc)` pair
   * two-argument element-wise with length-1 broadcast → `_BINARY`
     (`registry.py:263`), a `(name, doc)` pair
   * fixed signature → `_SIMPLE` (`registry.py:275`), a
     `(name, ret, args, doc)` tuple using the dimensionality codes from the
     module docstring (`registry.py:22`–`38`): a non-negative int is a fixed
     length, a negative int is a wildcard unified across the signature,
     `'string'` is the string type
   * a rule that cannot be written as a signature → `_SPECIAL`
     (`registry.py:351`), a `(name, ret, nargs, side_effect, doc)` tuple with
     `args=()` implied
2. `_build_table` (`registry.py:388`) picks the row up automatically, and every
   entry of `RELEASES` (`registry.py:439`) is rebuilt from it. If the name only
   exists in some releases, add a keyword knob to `_build_table` the way
   `include_pow` works — do **not** add a general filter, so that a second
   difference cannot slip through unnoticed.
3. If the function's arity differs in the historical table, add it to
   `_LEGACY_NARGS` (`registry.py:173`).
4. If the engine implements it but the front end rejects it, put it in that
   release's `backend_only` set instead (`registry.py:239`, `_LET_ONLY` at
   `437`). Never let `known()` return `True` for one of those.
5. **Gotcha:** `side_effect` for a `_SIMPLE` row is hard-coded as
   `side_effect=(name == "print")` at `registry.py:413`. A new side-effecting
   simple function needs that line changed, or `_reachability` will treat its
   arguments as dead code.

Nothing else needs touching. `sema._call_type` (`sema.py:563`) reads the table,
`_check_functions` (`sema.py:1111`) checks arity from `nargs`, and the GUI's
completion list reads `callable_` (`registry.py:202`).

Current tables, for reference:

| Release | Build | Validated | Functions | simple | thread | binary | special |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2025-3 | 160 | yes | 58 | 35 | 8 | 7 | 8 |
| 2020-3 | 139 | yes | 58 | 35 | 8 | 7 | 8 |
| legacy | – | no | 57 | 34 | 8 | 7 | 8 |
| generic | – | no | 58 | 35 | 8 | 7 | 8 |

Side-effecting: `declare_meta declare_output meta print store`. Backend-only
(2025-3): `let`.

### Add a diagnostic

**File: `funnelforge/core/lang/diagnostics.py`.**

1. Append a `_c(...)` row to the `_CODES` list (`diagnostics.py:182` through
   `786`) in the right family block. The id is three letters and three
   digits; the family prefix determines the layer via `CODE_FAMILIES`
   (`diagnostics.py:95`) and `MTD` is the only family allowed to span two
   layers. **Never reuse a retired id** — ids are part of the on-disk shape of
   session files (`diagnostics.py:157`–`160`).
2. `_validate_catalog` (`diagnostics.py:807`) runs at import and will refuse to
   let the module load if the id shape, layer, severity, family mapping, or the
   "an `info` code must not block" rule is violated. That is the fastest
   feedback you will get; run `python -c "import
   funnelforge.core.lang.diagnostics"` after editing.
3. Write `explain` for the person running the simulation — what the finding
   means for the run and what to do about it, never how the checker works
   (`diagnostics.py:158`–`160`).
4. Emit it. Two routes:
   * from a checker in `document.py`, call `diag.make("XXX999", message, ...)`
     (`diagnostics.py:1017`). `make` raises `KeyError` on an uncatalogued id, on
     purpose.
   * from the parser or `sema`, append a `cst.Problem(code, message, start,
     end, evidence)`. `Document._to_diag` (`document.py:390`) converts it. Note
     its fallback at `document.py:394`–`397`: a code not in the catalogue is
     silently downgraded to `SYN001` or `SEM001`, so a typo in a `Problem` code
     will not crash — it will quietly mislabel. Check your new code appears in
     `diag.CATALOG`.
5. If the finding should stop a run-ready export, set `blocks_export=True`;
   `Report.blocks_export()` (`diagnostics.py:1203`) and
   `Document.export_blockers` (`document.py:573`) pick it up with no further
   wiring. Same for `blocks_edit`.
6. Regenerate `docs/diagnostic-codes.md`, which is derived from `CATALOG`.

**The `official` layer is special.** `Report.add` deliberately refuses to
escalate it (`diagnostics.py:1130`–`1131`); only `Report.mark`, called by the
code that actually ran the engine, may set its state. Adding an `OFF` code does
not give the interface an opinion on validity, and `verdict()`
(`diagnostics.py:1230`) will keep saying "Desmond validation not performed"
until a real run says otherwise.

**Honest note on catalogue coverage.** The catalogue holds 100 codes; 42 are
referenced by a producer somewhere in `funnelforge/`, and **58 are declared but
never emitted by any code path in this tree**:

| Family | Never emitted | Of |
| --- | ---: | ---: |
| `LNT` | 15 | 17 |
| `SEM` | 13 | 18 |
| `SYN` | 10 | 18 |
| `PKG` | 10 | 10 |
| `MOD` | 5 | 8 |
| `TOP` | 3 | 8 |
| `MTD` | 2 | 11 |

The whole `package` layer is catalogued and never populated. Most of the lint
catalogue is likewise unimplemented — `Document._lint` (`document.py:354`)
currently emits `LNT006` (non-finite folded constant) and `LNT007`
(non-positive hill width) and nothing else. These are specifications of intended
checks, not descriptions of implemented ones; do not read the catalogue as a
feature list.

### Add a node kind

**File: `funnelforge/core/lang/cst.py`, then `sema.py`.**

1. Add the constant in the node-kind block (`cst.py:52`–`73`). If it is a
   statement, add it to `STATEMENT_KINDS` (`cst.py:76`).
2. Build it in `_Parser`. Statement forms go in `_statement_inner`
   (`cst.py:300`) and, if they may appear inside `{ }`, in `_block_body`
   (`cst.py:504`); expression forms go in `_atom` (`cst.py:454`). **Always
   construct through `self._node(kind, ti, ...)`** (`cst.py:208`) — it computes
   `start`/`end`/`ti`/`tj` from the tokens actually consumed, and getting those
   wrong is what `coverage_gaps` catches. Set `parent` on the children; `_node`
   does it for children passed in `children=`, and the call sites do it again
   for clarity.
3. Raise `_Recover(message, start, end)` (`cst.py:583`) for a malformed
   instance. Do not raise anything else; `_statement` only catches `_Recover`
   and `RecursionError`, and anything else will propagate out of `parse()` and
   break the "never rejects a file" property.
4. Teach `sema` about it, in this order:
   * `_free_names` (`sema.py:279`) — only if the node **binds** names, like
     `BLOCK` and `SERIES` do. If it merely contains expressions, the default
     recursion at `sema.py:310`–`311` already handles it.
   * `_type_of` (`sema.py:503`) — add a branch, or it silently yields
     `T_UNKNOWN`.
   * `_flatten_sum` (`sema.py:317`) and `_literal_number` (`sema.py:240`) — only
     if it is transparent to addition or can be a constant, the way `PAREN` is.
   * `const_value` (`sema.py:952`) and `coefficient_sign` (`sema.py:1016`) — if
     it can appear inside a hill-height expression. Both currently unwrap
     `PAREN`, handle `NUM`/`NAME`/`UNARY`/`BINARY`, and return `None` for
     everything else, which is the safe default.
   * `_walk_statements` (`sema.py:380`) — if it introduces a scope.
5. Render it. `source_window._refresh_structure` (`source_window.py:719`) builds
   the structure tree; `potcheck._analysis_overview` (`potcheck.py:58`) is the
   CLI equivalent.
6. Add a fixture to `tests/fixtures/`, register it in `manifest.json` with its
   bytes, sha256, encoding, newline style and what the real engine said, and let
   `test_every_fixture_parses_and_nothing_is_orphaned` and
   `test_open_and_save_is_byte_for_byte_identical` cover it.

The syntax highlighter (`source_window.py:66`) is regex-based and colour-only —
"the parser, not this, decides what anything means". It does not need updating
for correctness.

### Add a lint check

**File: `funnelforge/core/document.py`, method `_lint` (`document.py:354`).**

`_lint` receives the `Analysis` and emits diagnostics. It may read anything on
the analysis and must write nothing. The two existing checks are the pattern:

```python
for sym in an.symbols.values():
    v = sema.const_value(an, sym.value)
    if v is None:
        continue
    if v != v or v in (float("inf"), float("-inf")):
        line, col = self._pos(sym.bind.start)
        self.report.add(diag.make(
            "LNT006", f"`{sym.name}` folds to a non-finite value",
            evidence=sym.bind.span_text(self.source)[:120],
            file=self.path, line=line, col=col,
            span=(sym.bind.start, sym.bind.end)))
```

The tools worth knowing:

* `sema.const_value(an, node)` — fold to a number through the symbol table, or
  `None` if anything is runtime-dependent. This is how you check a parameter
  whose value is spread over several bindings.
* `sema.resolve_array(an, node)` — the elements of an array, through aliases.
* `an.metas` — every `meta()` call with its role, `wt`, `n_hills`, `n_cvs`.
* `an.contributions` — the additive terms of the potential, with `opaque` and
  `kinds`.
* `sym.reaches_energy` / `sym.reaches_side_effect` — do not warn about a value
  that reaches neither, and do not treat "not in the energy" as unused.
* `self._pos(offset)` (`document.py:403`) — offset → 1-based (line, col).
* Always pass `span=(start, end)`; the GUI uses it to select the offending text
  (`source_window.py:712`).

`_lint` runs inside `run_local_layers` (`document.py:306`–`307`), which then
marks the `lint` layer from the worst severity present (`_state`,
`document.py:382`). Do not call `report.mark("lint", ...)` yourself.

If the check needs a topology, it belongs in `resolve_topology`
(`document.py:410`) under a `TOP` code instead — the `lint` layer is marked
against the source revision alone and would go stale wrongly.

---

## Known gaps in this tree

Stated plainly, because a maintainer will otherwise find them the hard way.

* **Declaration term names are not validated.** `mexp.g:66`–`75` gives
  `declare_output` exactly three legal terms (`name`, `first`, `interval`) and
  `declare_meta` six. `cst._declare` (`cst.py:328`) accepts any `key = value`
  pair and `sema._check_declarations` (`sema.py:1130`) checks only `dimension`.
  So `declare_output(name = "o.cvseq", cutoff = 9.0);` passes this interface
  with no finding, and the real engine rejects it:

  ```
  $ /opt/schrodinger2025-3/run python3 -c "enhsamp.parseStr(cms, text)"
  line 1:33 no viable alternative at input 'cutoff'
  unable to parse m-expression
  ```

  A `SEM`-family check on the term keys, per declaration kind, is the fix.
* **`_divisor_magnitude` (`sema.py:1077`) is dead code.** Nothing calls it;
  `_ktemp_of` superseded it.
* **`cst.py:129` names a module that does not exist.** The docstring says
  `funnelforge.core.lang.glue` turns `Problem`s into catalogued `Diagnostic`s.
  There is no `glue.py`; the conversion is `Document._to_diag`
  (`document.py:390`).
* **`export_run_ready` writes the same bytes as `save_source`.**
  `document.py:634`–`636` encodes `self.source` unchanged. Today the only
  difference between the two paths is the set of gates in front of the strict
  one — there is no structured re-emission yet. When one is added, it is the
  moment every invariant in this document starts earning its keep, and
  `patch.unchanged_outside` is the thing that should gate it.
* **58 of the 100 catalogued codes have no producer** (table in §8), including
  the whole `package` layer and 15 of the 17 lint codes.

---

## Reading order for a new maintainer

1. `funnelforge/core/lang/__init__.py` — 23 lines, the layer contract.
2. `tests/test_lang.py` — the invariants, as executable statements.
3. `funnelforge/core/lang/lexer.py` — small, and the round-trip property is the
   foundation for everything above it.
4. `funnelforge/core/lang/cst.py` — read `_signed`/`_exp_comp` next to
   `mexp.g:83`–`90`, and `_opaque_from` next to `coverage_gaps`.
5. `funnelforge/core/lang/sema.py` from `analyse` (line 329) downward; the
   passes are in dependency order.
6. `funnelforge/core/document.py` — how the layers are driven and how results
   are pinned to a revision.
7. `docs/diagnostic-codes.md` for the catalogue, `docs/threat-model.md` for what
   the input is assumed to be.
