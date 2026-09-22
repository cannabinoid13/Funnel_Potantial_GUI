# Measured reports: round trip, differential, performance, security, fuzz

Everything below was run on this machine on 2026-08-28/29. No number here is
estimated or carried over from an earlier document; where something could not
be measured, the section says so.

**Machine and versions**

| | |
|---|---|
| CPU | Intel Core i5-14600K, 20 threads |
| RAM | 62 GiB |
| OS | Linux 6.8.0-138-generic, glibc 2.35 |
| Python | 3.10.13 (conda env `cugraph_env`) |
| Schrodinger | `/opt/schrodinger2025-3`, Suite 2025-3 Build 160 |
| Reference job | `/home/bugra/Downloads/funnel_metadaynamics_ayrilma_Z5_run.{pot,cms,msj,cfg,sh}` |
| Corpus | the 30 `.pot` fixtures in `tests/fixtures/` plus the reference `.pot` |

The tree was being edited by another agent while these measurements ran.
`document.py` and `potcheck.py` grew a package layer; `cst.py` and `sema.py`
were changed in response to the crash reported in 5.2. `_pos`,
`run_local_layers` and `save_source` were untouched throughout. Sections 1, 2,
4 and 5.1 were re-run against the final state and are reported from that run;
5.2 reports both states and names the digest of each. The digests are listed in
the last section. `tests/test_lang.py` passes 36/36 with
`FUNNELFORGE_OFFICIAL=1`.

**What the measurements found**

| | where |
|---|---|
| The lossless path is lossless: 31 of 31 files, byte for byte, including a BOM, CRLF, mixed endings and a missing final newline. | 1.1 |
| The older `Job` path rewrites 30 of the 31 `.pot` files it reads. That is correct for the files it generates and wrong for anything else, which is exactly why the `Document` path exists. | 1.3 |
| The interface and the installed engine agree on 28 of 31 files, and all three divergences are the engine rejecting something the interface allows -- never the other way round. | 2 |
| `SYN006` is graded `warning` for a construct the engine was measured to reject outright. | 2.3 |
| `Document._pos` rebuilds the whole line index once per diagnostic: 58 s on a 347 kB file with 4 358 diagnostics, 395 ms with the index memoised. | 3.2a |
| `ParseResult.opaque_nodes` walks the whole program once per additive contribution, so a **clean** 370 kB potential takes 33.6 s to analyse and 239 ms with the property memoised. | 3.2b |
| A flat sum of about 500 terms used to raise `RecursionError`; the fix that landed mid-report turns that into a **segmentation fault** between roughly 9 750 and 20 000 terms, which the GUI cannot catch. | 5.2 |
| The same `opaque_nodes` cost makes one long operator chain quadratic too: 122 ms at 500 terms, 26 s at 8 000, 154 ms memoised. | 5.2 |
| Every rejection in `safety` fires, no `eval`/`exec` exists anywhere, and the one subprocess is an argv list with no shell. | 4 |
| `minimal_env()` is correct and self-tested but has no caller outside its own module. | 4.2 |
| 150 000 fuzz iterations across two states of the code, zero failures of any invariant. | 5.1 |

---

## 1. Round trip

### 1.1 `Document.open` then `save_source`

Each file was copied to a temporary directory, opened through `Document.open`,
saved back with `save_source()` with no edit in between, and the bytes on disk
compared with the bytes that went in. The encoding column is what
`safety.sniff_encoding` decided; it is the codec name the code uses, not the
IANA name the manifest uses -- the code has no `us-ascii`, so a pure-ASCII file
is reported as `utf-8`.

| file | bytes | encoding | newline | final NL | identical |
|---|---:|---|---|---|---|
| `arbitrary_names.pot` | 1382 | utf-8 | LF | yes | yes |
| `arrays_indexing.pot` | 1740 | utf-8 | LF | yes | yes |
| `bad_accumulator.pot` | 861 | utf-8 | LF | yes | yes |
| `blocks_series_static.pot` | 1787 | utf-8 | LF | yes | yes |
| `bom_utf8.pot` | 699 | utf-8-sig (BOM) | LF | yes | yes |
| `comment_styles.pot` | 1613 | utf-8 | LF | **no** | yes |
| `crlf.pot` | 115 | utf-8 | **CRLF** | yes | yes |
| `deep_nesting.pot` | 1139 | utf-8 | LF | yes | yes |
| `dependency_cycle.pot` | 652 | utf-8 | LF | yes | yes |
| `dimension_mismatch.pot` | 1084 | utf-8 | LF | yes | yes |
| `duplicate_assign.pot` | 696 | utf-8 | LF | yes | yes |
| `empty_selection.pot` | 1344 | utf-8 | LF | yes | yes |
| `funnel_general.pot` | 3454 | utf-8 | LF | yes | yes |
| `future_function.pot` | 1440 | utf-8 | LF | yes | yes |
| `ifelse_chains.pot` | 1721 | utf-8 | LF | yes | yes |
| `meta_in_string.pot` | 1446 | utf-8 | LF | yes | yes |
| `metad_1d.pot` | 798 | utf-8 | LF | yes | yes |
| `metad_2d_wt.pot` | 1888 | utf-8 | LF | yes | yes |
| `metad_3d.pot` | 1089 | utf-8 | LF | yes | yes |
| `minimal.pot` | 113 | utf-8 | LF | yes | yes |
| `mixed_endings.pot` | 526 | utf-8 | **mixed** | yes | yes |
| `multi_accumulator.pot` | 2050 | utf-8 | LF | yes | yes |
| `multiline_calls.pot` | 1814 | utf-8 | LF | yes | yes |
| `nested_meta.pot` | 1905 | utf-8 | LF | yes | yes |
| `no_final_newline.pot` | 755 | utf-8 | LF | **no** | yes |
| `no_metad.pot` | 992 | utf-8 | LF | yes | yes |
| `scientific_numbers.pot` | 1779 | utf-8 | LF | yes | yes |
| `undefined_symbol.pot` | 754 | utf-8 | LF | yes | yes |
| `unicode_comments.pot` | 1965 | utf-8 | LF | yes | yes |
| `walls_only.pot` | 2132 | utf-8 | LF | yes | yes |
| `funnel_metadaynamics_ayrilma_Z5_run.pot` | 3472 | utf-8 | LF | yes | yes |

31 of 31 byte-identical; SHA-256 in equals SHA-256 out in every case. The four
awkward properties in the corpus -- a BOM, CRLF, a mixture of CRLF and LF, and
a missing final newline -- all survive, which is the point of carrying an
`Encoding` rather than normalising on read.

The two non-`.pot` files in the fixtures directory went through the same path
as a control, since `Document.open` does not care about the extension:

```
manifest.json          31535 bytes  utf-8  LF  final_nl=True   identical=True
differential.json       3677 bytes  utf-8  LF  final_nl=False  identical=True
```

### 1.2 The four job files through the older `Job` path

`Job.import_bundle` on the reference `.pot` discovers and loads all five files
(`.cms`, `.pot`, `.cfg`, `.msj`, `.sh`) in 0.05 s -- the `.cms` comes from the
pickle cache in `~/.cache/funnelforge/`; a cold read is measured in section 3.5.
Each file was then rendered back out through whatever route that pipeline uses
for it, and compared with what was read.

| file | bytes in | bytes out | route out | identical |
|---|---:|---:|---|---|
| `funnel_metadaynamics_ayrilma_Z5_run.pot` | 3472 | 3472 | `emit_pot(spec)`, regenerated from the model | yes |
| `funnel_metadaynamics_ayrilma_Z5_run.msj` | 3921 | 3921 | `MsjFile.text`, the unedited `BlockText` buffer | yes |
| `funnel_metadaynamics_ayrilma_Z5_run.cfg` | 1609 | 1609 | `CfgFile.text`, the unedited `BlockText` buffer | yes |
| `funnel_metadaynamics_ayrilma_Z5_run.sh` | 480 | 480 | `ShFile.render()`, regex patch in place | yes |

That pipeline is unaffected: all four come back identical.

The `.pot` row needs a caveat, because it is the one that is not a buffer being
handed back. `Job.pot_text()` calls `emit_pot(spec)`, which renders the
canonical funnel template from the parsed `FunnelSpec`; `spec.source_text` is
stored by `parse_pot` and never read again. It really is a regeneration --
changing one field changes exactly one line:

```
emit_pot regenerates (it does not echo spec.source_text): changing
spec.kernel_cutoff by +1 changed 1 line(s):
   line 7:  'cutoff = 9.0,' -> 'cutoff = 10.0,'
```

It comes back identical because this particular job file is already in exactly
the form `emit_pot` writes.

### 1.3 What that older `.pot` route does to everything else

Running `parse_pot` then `emit_pot` over the whole corpus, the way `Job` does
(text mode, so universal newlines have already collapsed CRLF before the parser
sees it):

```
parse_pot + emit_pot over 31 files
  byte identical : 1  ['funnel_metadaynamics_ayrilma_Z5_run.pot']
  changed        : 30
  raised         : 0
```

with, for example:

| file | chars in | chars out |
|---|---:|---:|
| `minimal.pot` | 113 | 1452 |
| `walls_only.pot` | 2132 | 1314 |
| `funnel_general.pot` | 3454 | 1602 |
| `arrays_indexing.pot` | 1740 | 1287 |
| `future_function.pot` | 1440 | 2050 |

A 113-byte `minimal.pot` comes back as a 1452-character canonical funnel
potential. Nothing crashed and nothing was reported as an error: the file was
simply replaced by the interface's idea of what a `.pot` looks like. That is
the loss the `Document` path in 1.1 exists to prevent, and it is why the two
paths must not be confused. `Job` is the funnel builder and owns files it
generated; `Document` is the editor and owns files somebody else wrote.

---

## 2. Differential against the installed engine

Re-run from scratch, not copied from `tests/fixtures/differential.json`. For
each file: `Document.open`, `run_local_layers()`, `resolve_topology(cms)` for
the interface's own verdict, then `adapter.validate(source, cms)` for the
engine's. "interface" below is the highest severity the interface reported;
"engine" is `enhsamp.parseStr(cms_model, text)` run under
`/opt/schrodinger2025-3/run` against the reference `.cms` (62 870 atoms).
Engine wall time per file across the 31 runs: median 1.90 s, min 1.86 s,
max 1.97 s.

| fixture | interface | engine | engine message | agree |
|---|---|---|---|---|
| `arbitrary_names.pot` | error SYN001 | rejected | no viable alternative at input 'static' | agree |
| `arrays_indexing.pot` | warning TOP008 | accepted | -- | agree |
| `bad_accumulator.pot` | error MTD003 | rejected | metadynamics accumulator id outsides range of accumulators | agree |
| `blocks_series_static.pot` | clean | accepted | -- | agree |
| `bom_utf8.pot` | warning SYN013 | accepted | -- | agree |
| `comment_styles.pot` | warning SYN006 | rejected | no viable alternative at character '&lt;EOF&gt;' | **differ** |
| `crlf.pot` | clean | accepted | -- | agree |
| `deep_nesting.pot` | clean | rejected | maximum recursion depth exceeded while calling a Python object | **differ** |
| `dependency_cycle.pot` | error SEM003 | rejected | unknown variable 'b' | agree |
| `dimension_mismatch.pot` | error MTD004, MTD005 | rejected | metadynamics accumulator 0 has dimension 2 but was passed a length-3 array collective variable | agree |
| `duplicate_assign.pot` | error SEM002 | rejected | k_wall declared twice in the same scope | agree |
| `empty_selection.pot` | error TOP001, TOP002 | rejected | mmasl_parse_input returned error code -1 (unknown) for arguments ('resname ZZZZ', Bitset(13), Structure(1), 1) | agree |
| `funnel_general.pot` | warning MTD008 | accepted | -- | agree |
| `future_function.pot` | warning MOD003, MTD008 | rejected | unknown function 'quantum_cv' | **differ** |
| `ifelse_chains.pot` | clean | accepted | -- | agree |
| `meta_in_string.pot` | clean | accepted | -- | agree |
| `metad_1d.pot` | warning MTD008 | accepted | -- | agree |
| `metad_2d_wt.pot` | clean | accepted | -- | agree |
| `metad_3d.pot` | warning MTD008 | accepted | -- | agree |
| `minimal.pot` | clean | accepted | -- | agree |
| `mixed_endings.pot` | clean | accepted | -- | agree |
| `multi_accumulator.pot` | warning MTD008 | accepted | -- | agree |
| `multiline_calls.pot` | warning MTD008 | accepted | -- | agree |
| `nested_meta.pot` | warning MTD008 | accepted | -- | agree |
| `no_final_newline.pot` | clean | accepted | -- | agree |
| `no_metad.pot` | clean | accepted | -- | agree |
| `scientific_numbers.pot` | clean | accepted | -- | agree |
| `undefined_symbol.pot` | error SEM001 | rejected | unknown variable 'ghost_offset' | agree |
| `unicode_comments.pot` | warning MTD008 | accepted | -- | agree |
| `walls_only.pot` | clean | accepted | -- | agree |
| `funnel_metadaynamics_ayrilma_Z5_run.pot` | clean | accepted | -- | agree |

**28 of 31 agree. All three divergences run the same way: the engine rejects
and the interface does not.** Nothing in this corpus is rejected by the
interface and accepted by the engine, which is the direction that would matter,
because it is the direction in which the interface would be telling a chemist
that a working potential is broken.

### 2.1 `future_function.pot` -- deliberate, and the interface is right

The engine says `unknown function 'quantum_cv'`. The interface says, twice, at
the right places:

```
warning MOD003 30:7  `quantum_cv()` is not in the 2025-3 function registry; it is
                     preserved unchanged and left for the official validator to judge
warning MOD003 33:8  `smart_wall()` is not in the 2025-3 function registry; ...
```

This is the designed behaviour, not a gap. The function table in `registry.py`
describes one release; a `.pot` written for a newer suite, or one using a
function the table has not caught up with, must not be rejected by a table that
is by construction out of date. The call is preserved verbatim, the statement
does not become opaque (`test_unknown_functions_are_preserved_not_rejected`
asserts both), and the export gate refuses anyway until the official layer has
passed -- so nothing built on that call can be exported as run-ready while the
engine is unhappy with it. Note the interface also flags `smart_wall()`, which
the engine never reached because it stopped at the first unknown name.

### 2.2 `deep_nesting.pot` -- the interface is right about the grammar, and the engine still cannot compile it

200 redundant parenthesis pairs around one expression. Under `mexp.g` this is
legal, and the interface parses it cleanly to a CST 203 deep, against its
ceiling of `Limits.max_ast_depth = 256`. The engine raises. Running the worker
directly and reading the whole traceback back -- 76 frames, 16 412 characters --
gives the cause exactly:

```
RecursionError: maximum recursion depth exceeded while calling a Python object
  enhsamp.py:749 parseStr -> enhsamp.py:698 procText -> mexpParser.prog
  then repeating: expr -> factor -> signedExpComp -> expComp -> comp -> atom -> expr
  frame counts before truncation:
  Counter({'expr': 12, 'factor': 12, 'signedExpComp': 12,
           'expComp': 11, 'comp': 11, 'atom': 11, ...})
```

Six Python frames per parenthesis level in an ANTLR3 recursive-descent
recogniser, 200 levels, against CPython's default limit of 1000 in the suite's
own 3.11. This is a limit of the vendor implementation, not of the language,
and `tests/fixtures/manifest.json` already records the same conclusion under
`expect_parses`.

Bisecting that ceiling against the live engine, with one expression wrapped in
N parenthesis pairs and everything else held constant: **the engine accepts 163
and raises `RecursionError` at 164.** That is 978 frames at six per level,
against a limit of 1000, so the arithmetic in the fixture's own comment checks
out. The interface parses 164 without complaint -- `syntax: passed`, the only
diagnostic being `MTD001` ("no meta() call was found").

The honest qualification: the engine is the thing that runs the job. A file the
engine cannot compile cannot run, however legal it is. The interface reports
this file as clean with no note at all, and only the official layer says
otherwise. A note along the lines of "this nests N levels deep; the installed
2025-3 parser overruns its stack at 164" would be worth having, and does not
exist today.

### 2.3 `comment_styles.pot` -- the same fact, graded too softly

The interface finds it, names the cause, and points at the exact line:

```
warning SYN006 42:1  this comment is the last thing in the file and is not followed
                     by a newline; Desmond's lexer defines a comment as running up to
                     a newline and rejects the file without one
```

The engine's verdict on that same file is `no viable alternative at character
'<EOF>'`, a rejection. The catalogue entry hedges ("can be rejected by the
engine") and sets `blocks_edit=False, blocks_export=False`. On the one file in
this corpus that has the construct, the engine does not "can": it does.
`no_final_newline.pot` shows the distinction is real and narrow -- it also lacks
a final newline, its last line is not a comment, and the engine accepts it.

This is the one divergence I would change. SYN006 describes a construct that was
measured to be fatal, and grading it `warning` understates it. Raising it to
`error`, or at minimum setting `blocks_export=True`, would make the interface
agree with the engine everywhere except the two cases where disagreeing is the
point. Nothing unsafe follows from the current grading -- OFF001 blocks a
run-ready export until the official parser has actually passed -- but a user
reading the panel is told "warning" about something that will not run.

### 2.4 What changed since `tests/fixtures/differential.json` was written

The stored file has 30 rows; it predates the reference job being included.
Comparing the 30 shared rows, two moved and no engine verdict changed:

| fixture | stored | re-run | why |
|---|---|---|---|
| `arbitrary_names.pot` | interface: no error | interface: **error SYN001** | a real change in the interface. The fixture has `static keeper(1);` on line 29, after the body has started; the interface now enforces Desmond's `header block` ordering before the engine does, and agrees with the engine's `no viable alternative at input 'static'`. `test_the_engines_grammar_rules_are_enforced_before_the_engine` covers it. |
| `empty_selection.pot` | interface: no error | interface: **error TOP001, TOP002** | not a change in the interface. The stored run did not resolve the topology; this one does, and the topology layer reports that `ghost` selects no atoms and `past_end` names atom 999999, outside 1..62870. Without a `.cms` the interface still reports nothing here. |

The stored file is stale on the first row and should be regenerated.

---

## 3. Performance

Each figure is the median of 5 repetitions unless the table says otherwise,
with `gc.collect()` before each. The synthetic files are the reference job
concatenated 10, 100 and 1000 times, as specified. Concatenation is not a
neutral way to grow a file: the reference job binds 40 names, so every copy
after the first adds 40 duplicate-binding errors -- 360 of them at x10, 3 960 at
x100. Section 3.2 measures what that costs and separates it from the front
end's own scaling.

### 3.1 The four sizes

| file | bytes | statements | tokenize | parse | analyse | `run_local_layers` |
|---|---:|---:|---:|---:|---:|---:|
| real x1 | 3 472 | 55 | 0.7 ms | 1.5 ms | 1.2 ms | 3.0 ms |
| real x10 | 34 720 | 550 | 7.2 ms | 14.0 ms | 9.2 ms | 552 ms |
| real x100 | 347 200 | 5 500 | 87.3 ms | 175.6 ms | 90.7 ms | 58 257 ms |
| real x1000 | 3 472 000 | 55 000 | 846 ms | 1 698 ms | 1 013 ms | @@RUNLOCAL1000@@ |

`tokenize`, `parse` and `analyse` are linear to within measurement noise:
tokenize goes 0.7 ms to 846 ms and parse 1.5 ms to 1 698 ms across a 1000-fold
size increase. The 3.47 MB file lexes to 715 001 tokens; `Limits.max_tokens` is
2 000 000, so this is inside the ceiling.

`run_local_layers` is not linear. It goes 3.0 ms, 552 ms, 58.3 s across the same
range -- roughly quadratic.

### 3.2 Where that time goes: two independent quadratics

`run_local_layers` has two superlinear costs, and they are hit by different
kinds of file, so a benchmark that grows a file one way will only find one of
them. Both were located by profiling and both were quantified by installing a
one-line cache **in the measuring process only**, by rebinding the attribute;
nothing in the repository was touched.

#### 3.2a The line index is rebuilt once per diagnostic

Profiling `run_local_layers` on the 34.7 kB concatenated file, 0.829 s total:

```
   ncalls  tottime  cumtime  filename:lineno(function)
        1    0.001    0.833  document.py:243(run_local_layers)
      776    0.002    0.765  document.py:419(_pos)
     1176    0.001    0.762  lang/lexer.py:260(position)
      777    0.744    0.762  lang/lexer.py:251(line_starts)   <-- 90% of the run
      378    0.001    0.747  document.py:406(_to_diag)
        3    0.000    0.037  document.py:206(parsed)
        1    0.000    0.037  lang/cst.py:591(parse)
        1    0.000    0.024  lang/sema.py:329(analyse)
```

`Document._pos` calls `lexer.position(self.source, offset)` without the optional
precomputed index, and `position` then rebuilds it from scratch:

```python
def line_starts(src: str) -> list:
    """Offset of the first character of every line, for offset -> line/col."""
    out = [0]
    for i, ch in enumerate(src):
        if ch == "\n":
            out.append(i + 1)
    return out

def position(src: str, offset: int, starts: list | None = None) -> tuple:
    starts = starts if starts is not None else line_starts(src)
```

The cost is O(file size x number of diagnostics). Memoising the index for the
duration of one `run_local_layers`:

| file | bytes | diagnostics | as shipped | with a memoised line index |
|---|---:|---:|---:|---:|
| real x1 | 3 472 | 2 | 3.0 ms | 2.8 ms |
| real x10 | 34 720 | 398 | 565 ms | 26.1 ms |
| real x100 | 347 200 | 4 358 | 58 784 ms | 395 ms |

149x on the 347 kB file, and peak memory is unchanged (30.3 MB shipped against
30.2 MB memoised -- measured, 3.4). `position()` already takes a `starts`
argument for exactly this purpose; `Document._pos` is the one caller that does
not pass it.

This one needs a file that is both large and full of diagnostics -- which is
what a chemist gets when they paste something large that does not quite parse.

#### 3.2b The opaque-node list is recomputed once per contribution

The concatenated synthetic hides the second one, because it has only three
additive contributions no matter how many times it is repeated. Growing a file
the other way -- a *clean* potential with unique names that sums many terms,
which is what a generated per-residue restraint set looks like -- gives:

| file | bytes | statements | diagnostics | tokenize | parse | analyse | `run_local_layers` |
|---|---:|---:|---:|---:|---:|---:|---:|
| clean, n=55 | 4 528 | 173 | 1 | 2.6 ms | 5.2 ms | 10.3 ms | 16.0 ms |
| clean, n=400 | 34 529 | 1 214 | 1 | 18.0 ms | 37.6 ms | 368 ms | 404 ms |
| clean, n=3 980 | 370 224 | 12 028 | 2 | 194.3 ms | 407.2 ms | **33 676 ms** | **34 650 ms** |

Tokenize and parse are linear. `analyse` is not: 10 ms, 368 ms, 33.7 s for an
8x then 11x growth. The tree is only 52 deep here, so this is nothing to do
with the recursion problem in 5.2. Profiling `sema.analyse` at 800 terms,
4.39 s total:

```
     ncalls  tottime  cumtime  filename:lineno(function)
   12350068    2.409    3.433  lang/cst.py:101(walk)
        801    0.888    4.282  lang/cst.py:130(<listcomp>)      <-- Node.find
          1    0.007    4.298  lang/sema.py:745(_reachability)
          1    0.008    0.042  lang/sema.py:433(_collect_symbols)
```

801 calls to `Node.find` and 12.35 million `walk` steps. The cause:

```python
    @property
    def opaque_nodes(self) -> list:          # cst.py, ParseResult
        return self.program.find(OPAQUE)

    def find(self, kind: str):               # cst.py, Node
        return [n for n in self.walk() if n.kind == kind]
```

`opaque_nodes` is a property that walks the entire program every time it is
read, and `sema._touches_opaque` reads it once for every additive contribution:

```python
def _touches_opaque(a: Analysis, term: Node, sym: Symbol | None) -> bool:
    """Does this contribution depend on anything the parser could not read?"""
    opaque_spans = [(n.start, n.end) for n in a.parse.opaque_nodes]
```

so the walk count is (number of contributions) x (size of the program).
Memoising the property on the class, again in the measuring process only:

| terms | bytes | statements | contributions | `analyse` as shipped | with `opaque_nodes` memoised |
|---:|---:|---:|---:|---:|---:|
| 100 | 8 166 | 308 | 100 | 29 ms | 6 ms |
| 400 | 34 529 | 1 214 | 400 | 373 ms | 25 ms |
| 800 | 69 701 | 2 422 | 800 | 1 426 ms | 78 ms |
| 1 600 | 144 253 | 4 838 | 1 600 | 5 073 ms | 136 ms |
| 3 980 | 370 224 | 12 028 | 3 980 | 33 614 ms | 239 ms |

141x at 370 kB, and the memoised column is linear. `ParseResult` is created
once per parse and its `program` never changes afterwards, so caching the list
is safe; `functools.cached_property`, or computing it in `parse()` alongside
`problems`, would do it.

Between them, 3.2a and 3.2b are the whole gap between the linear front end
(tokenize, parse) and the superlinear `run_local_layers` in 3.1.

### 3.3 Incremental re-parse after a one-character edit

There is no incremental parser. `Document.set_source` calls `_invalidate()`,
which drops the cached `ParseResult` and `Analysis` outright, and the next
`parsed()` re-parses the whole file. Appending one space and re-deriving:

| file | bytes | `set_source` + `parsed()` | `set_source` + `run_local_layers()` |
|---|---:|---:|---:|
| real x1 | 3 472 | 1.5 ms | 2.9 ms |
| real x10 | 34 720 | 14.9 ms | 573 ms |
| real x100 | 347 200 | 176 ms | 58 860 ms (1 repetition) |
| real x1000 | 3 472 000 | @@REPARSE1000@@ | not measured |

A one-character edit costs exactly a cold parse; the numbers match 3.1 to
within noise. That is honest, but it is the whole cost model -- there is no
reuse of any kind between revisions.

### 3.4 Memory

`tracemalloc`, one repetition each. `tracemalloc` itself slows the traced code
by roughly 7x (the 347 kB shipped `run_local_layers` takes 58.3 s untraced and
403 s traced), so the durations behind this table are not comparable with 3.1
and are not reproduced here.

| file | stage | peak | still held afterwards |
|---|---|---:|---:|
| 347 200 B | `tokenize` | 18.2 MB | 18.2 MB |
| 347 200 B | `cst.parse` | 27.2 MB | 26.1 MB |
| 347 200 B | `sema.analyse`, parse result already held | 2.5 MB | 2.0 MB |
| 347 200 B | `Document.from_text` + `run_local_layers`, as shipped | 30.3 MB | 29.9 MB |
| 347 200 B | the same, with the memoised line index | 30.2 MB | 30.2 MB |
| 3 472 000 B | `tokenize` | 181.7 MB | 181.7 MB |
| 3 472 000 B | `cst.parse` | 271.5 MB | 260.7 MB |
| 3 472 000 B | `sema.analyse`, parse result already held | 23.9 MB | 18.2 MB |
| 3 472 000 B | `Document.from_text` + `run_local_layers` | **301.9 MB** | 301.9 MB |

Peak for the 3.4 MB case is 301.9 MB, about 87 bytes of Python objects per byte
of source, which is what 715 001 `Token` dataclasses and a CST over them cost.
Process RSS peaked at 917 MB during that run, which includes NumPy and the
interpreter. Memory is linear in file size and is not the limit here; time is.

### 3.5 Topology resolution against the real `.cms`

| step | median | notes |
|---|---:|---|
| `Structure.load(cms, use_cache=False)` | 0.92 s (median of 3) | 48 801 936 bytes, 62 870 atoms, file already in the page cache |
| `Structure.load(cms)` from the pickle cache | 0.021 s | `~/.cache/funnelforge/f47d6504395e37927805.pkl`, 10 215 985 bytes |
| `Document.resolve_topology(structure=st)` | 0.6 ms | structure already in hand |
| `Document.open` + `run_local_layers` + `resolve_topology(path)` | 0.06 s | end to end, warm cache |

The five selections in the reference job resolve to:

```
lig            n=72     chains=['B']
site           n=16     chains=['A']
core           n=16     chains=['A']
tyr114_oh_sel  n=1      chains=['A']
tyr197_oh_sel  n=1      chains=['A']
```

Resolution itself is negligible. The cost is reading the structure, and the
pickle cache turns that from 0.9 s into 0.02 s after the first open.

### 3.6 One official validation

Five runs of `adapter.validate(pot, cms)` on the reference job:

```
wall: 1.84, 1.86, 1.81, 1.81, 1.85 s   median 1.84 s   ok=True exit=0
inside the worker: read_cms 1.24 s, enhsamp.parseStr 0.0115 s
compiled_chars 3476
```

Of 1.84 s, 1.24 s is the suite reading the 48 MB `.cms` and 11.5 ms is the parse
itself; the remaining ~0.6 s is the `run` wrapper and interpreter start-up. The
manifest recorded for that run:

```json
{"pot_sha256": "4bf672d5648d1f7db20a70f972d2cee4a5b5fb3967ff13690f41f9a281611e6e",
 "pot_bytes": 3472,
 "cms_sha256": "5ac80af085e7f5d4951ffdc3b3be4c7b3ebc72891eaaac6c82a158c9e93043bf",
 "schrodinger_path": "/opt/schrodinger2025-3", "version": "2025-3", "build": "160",
 "argv": ["/opt/schrodinger2025-3/run",
          ".../funnelforge/core/desmond/_worker.py",
          ".../funnel_metadaynamics_ayrilma_Z5_run.cms",
          "/tmp/funnelforge-official-9lxlds85/potential.pot"],
 "exit_code": 0, "duration_s": 1.8408, "read_cms_s": 1.214, "parse_s": 0.0115,
 "compiled_chars": 3476}
```

Nothing on this side can make that much faster: the topology read dominates and
it happens inside the suite's own process.

### 3.7 Where it stops being interactive

The GUI debounces typing by 250 ms (`SourceWindow._debounce`,
`setInterval(250)`) and runs `run_local_layers` and `resolve_topology` on a
`QThread` (`AnalysisWorker`), so the UI thread never blocks regardless of file
size. What degrades is how far the panel lags behind the cursor.

| file | feedback after a keystroke | usable |
|---|---|---|
| 3.5 kB, a real job | 250 ms debounce + 3 ms | yes, indistinguishable from instant |
| 35 kB, concatenated, 398 diagnostics | 250 ms + 570 ms | yes, just |
| 35 kB, clean, 400 contributions | 250 ms + 400 ms | yes, just |
| 350 kB, concatenated, 4 358 diagnostics | 250 ms + **59 s** | no |
| 350 kB, clean, 3 980 contributions | 250 ms + **35 s** | no |
| 3.5 MB | 1.7 s to parse alone, before any analysis | no |

Plainly: **the interface is interactive to about 35 kB and stops being
interactive somewhere between there and 350 kB, and it stops for two separate
reasons -- 3.2a if the file is full of diagnostics, 3.2b if it is clean but sums
many terms.** Both are quadratic and both are caches that are missing rather
than algorithms that are wrong: with the two caches in place the same 350 kB
files take 395 ms and 239 ms.

Real `.pot` files are single-digit kilobytes, so nobody is hitting this today.
A generated potential with one restraint per residue on a 4 000-residue system
would be exactly the 350 kB clean case.

The official layer is a separate matter: 1.84 s, off the UI thread, cancellable
(`OfficialWorker.stop()` sets the event, and section 4 shows the process group
is killed), and run on demand rather than while typing.

---

## 4. Security

### 4.1 Every rejection in `funnelforge.core.safety`, exercised

| threat | test | result |
|---|---|---|
| oversized file | 9 000-byte `.pot`, `Limits(max_file_bytes=100)` | `LimitExceeded: .../big.pot: file is larger than the limit of 100 bytes` |
| zip path traversal | member `../escape.pot` | `UnsafeArchive: archive member '../escape.pot' traverses out of the destination directory`; `../escape.pot` was not created |
| deeper traversal | member `ok/../../escape2.pot` | `UnsafeArchive: ... traverses out of the destination directory` |
| absolute member | member `/etc/ff_pwned.pot` | `UnsafeArchive: archive member '/etc/ff_pwned.pot' is an absolute path` |
| Windows drive-absolute member | member `C:/windows/ff.pot` | `UnsafeArchive: ... is an absolute path` |
| symlink member | `ZipInfo` with `create_system=3`, mode `S_IFLNK\|0o777`, body `/etc/passwd` | `UnsafeArchive: archive member 'link.pot' is a symbolic link; only regular files and directories are extracted` |
| write through a planted symlink | destination pre-seeded with `planted.pot -> /tmp/ff_should_not_exist`, archive contains `planted.pot` | `UnsafeArchive: ... would overwrite the existing .../planted.pot`; `/tmp/ff_should_not_exist` was not created |
| too many members | 50 entries, `Limits(max_archive_members=10)` | `LimitExceeded: ...: number of archive members: 50 exceeds the limit of 10` |
| compression ratio bomb | 8 MiB of zeros deflated to 8 157 bytes, ratio 1028:1, default limit 200 | `LimitExceeded: archive member 'bomb.pot' compression ratio: 1028.39 exceeds the limit of 200` |
| archive over the total budget | the same archive, `Limits(max_archive_bytes=1024)` | `LimitExceeded: ...: archive file size: 8271 exceeds the limit of 1024` |
| backslash in a member name | member `a\b.pot` | `UnsafeArchive: ... contains a backslash; ZIP names use '/' and a backslash is a directory separator on Windows` |
| Windows device name | member `CON.pot` | `UnsafeArchive: archive member 'CON.pot' names the reserved Windows device CON` |
| duplicate name, case-insensitive | members `A.pot` and `a.pot` | `UnsafeArchive: archive contains 'a.pot' twice (names are compared case-insensitively)` |
| a benign archive still extracts | member `run/ok.pot` | extracted to `run/ok.pot`, mode `0600` |
| shell metacharacters in the `.cms` path | `validate(pot, "/tmp/nope; touch /tmp/ff_pwned")` | refused, `reason=topology-unreadable`, `topology not found: /tmp/nope; touch /tmp/ff_pwned`; `/tmp/ff_pwned` was not created |
| command substitution in the `.cms` path | a path containing a backtick-quoted `touch /tmp/ff_pwned2` | refused, `topology not found: ...`; `/tmp/ff_pwned2` was not created |
| `.cms` is a FIFO | `os.mkfifo` | refused, `topology is not a regular file: .../pipe.cms` |
| `.cms` is a symlink to a device | `link.cms -> /dev/zero` | refused, `topology is not a regular file: /dev/zero` -- the message names the resolved target, not the link |
| `.cms` is a directory | | refused, `topology is not a regular file: ...` |
| no `.cms` at all | `validate(pot, None)` | refused, `reason=topology-required`, `ran=False` -- not reported as a rejection of the potential |
| runaway child | `validate(..., timeout=0.5)` on the real job | `timed_out=True` after 0.54 s, `official validation timed out after 0.500 s and the process group was killed`; 3 s later, zero processes matching `_worker.py` or `schrodinger2025-3/run` remain |
| cancellation | a `threading.Event` already set | refused before the spawn, `reason=cancelled` |

`python -m funnelforge.core.safety` -- the module's own self-test, which covers
these plus forged CRC headers, encrypted members, members under a symlinked
subdirectory, and the encoding round trips -- reports `148 passed, 0 failed`.

### 4.2 `minimal_env()`

Printed verbatim from `safety.minimal_env()` in this shell:

```
  HOME               /home/bugra
  LANG               en_US.UTF-8
  LOGNAME            bugra
  PATH               /home/bugra/miniconda3/envs/cugraph_env/bin:...:/usr/bin:/bin:...
  PYTHONNOUSERSITE   1
  USER               bugra
  entries: 6
  parent os.environ entries: 107
  dropped from parent: 102
  examples dropped: AI_AGENT, AMBERHOME, ANTHROPIC_BASE_URL, BAGGAGE, CHROME_DESKTOP,
                    CLAUDECODE, CLAUDE_AGENT_SDK_VERSION, ...
  os.environ mutated?  False
```

Six variables out of 107, and `os.environ` is not touched. Relative `PATH`
entries are stripped:

```
  in : /usr/bin::.:relative/bin:/bin
  out: /usr/bin:/bin
```

The self-test additionally confirms that `LD_PRELOAD`, `PYTHONPATH` and
`BASH_ENV` are dropped and `SCHRODINGER` is kept when it is set.

Two things a maintainer should know about this function.

* It is minimal in the sense of *allow-listed*, not in the sense of *empty*.
  `PATH` is inherited from the parent with only relative entries removed, so in
  this shell it still carries the conda prefix. Six entries is the floor here,
  not a fixed set.
* **Nothing outside `safety.py` calls it.** `grep -rn minimal_env` finds only
  the module's own docstring and self-test. The one place in the tree that
  actually starts a child process, `adapter._worker_environment`, builds its own
  dictionary from scratch and is stricter: it hard-codes
  `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, points `TMPDIR`, `TMP` and `TEMP` at the
  per-run temporary directory, and forwards only `HOME`, `LANG`,
  `SCHROD_LICENSE_FILE` and `LM_LICENSE_FILE`. So the conda `PATH` above never
  reaches the suite. `minimal_env()` is correct and tested but currently unused;
  it should either be adopted by the adapter or documented as the helper for a
  future child process.

### 4.3 No `eval` or `exec`

The grep asked for, run at the repository root:

```
$ grep -rn "\beval(\|\bexec(" funnelforge/
$ echo $?
1
```

No output, exit status 1: no match anywhere in the package. Broadening it finds
nothing either -- no `os.system`, no `popen`, no `shell=`, and every `compile(`
in the tree is `re.compile`. The M-expression is never turned into Python, in
this process or in the worker; `_worker.py` states that in its own docstring and
hands the text to `enhsamp.parseStr` as an opaque string.

### 4.4 The subprocess uses an argv list

`adapter._spawn`, quoted in full:

```python
def _spawn(argv: list, env: dict, workdir: str, timeout: float, cancel) -> tuple:
    """Run the worker. Returns (exit_code, stdout, stderr, timed_out, cancelled).

    No shell, no ``shell=True``, no string command: ``argv`` goes to ``execve``
    exactly as given.
    """
    proc = subprocess.Popen(
        argv,
        cwd=workdir,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
```

`argv` is built by `Installation.argv_for`, which returns a list and nothing
else:

```python
def argv_for(self, worker_path: str, cms_or_dash: str, pot_path: str) -> list[str]:
    """The exact argument list used to invoke the worker."""
    return [self.runner, worker_path, cms_or_dash, pot_path]
```

Checked at run time: `type(argv).__name__` is `list`, contents
`['/opt/schrodinger2025-3/run', '/w.py', '/x.cms', '/y.pot']`. There is no
`shell=True` and no string command anywhere in the package (4.3).

The potential itself is never an argument. `validate` writes it to a fixed name
`potential.pot` inside a fresh `tempfile.mkdtemp` and passes that path, so no
part of the user's text can appear on a command line. The `.cms` path is the
only caller-supplied argument, and it has been through `_canonical_cms`
(`realpath`, must exist, must be a regular file, must be readable) before it
gets there; 4.1 shows what that refuses.

The suite's `run` wrapper is itself a `/bin/sh` script, but it is reached
through `execve` with an argument vector, so the arguments are never re-parsed
by a shell.

### 4.5 Not covered by this section

`Structure.load` unpickles a cache from `~/.cache/funnelforge/` when one exists
(`cms.py`, `_cache_path`). That is a trust boundary inside the user's own home
directory rather than one crossed by a downloaded file, so it is out of scope
for the rejection list above, but it is the one place in the tree that
deserialises anything. `docs/threat-model.md` is the right place for it.

---

## 5. Fuzz

### 5.1 Three campaigns, 75 000 iterations

`tests/test_lang.py::test_fuzzing_never_crashes_hangs_or_loses_text` was copied
into the scratchpad and run at 5 000 iterations per seed instead of 400.
Nothing in the repository was modified. Three campaigns, five seeds each.

* **A -- the test as written.** Seeds from the first 12 fixtures; 1 to 12
  character-level deletions, insertions from a 47-item alphabet, and run
  reversals per iteration.
* **B -- harsher.** All 30 fixtures plus the reference job as seeds; 1 to 120
  mutations per iteration; alphabet extended with NUL, a BOM, CR, an emoji and
  runs of 60 brackets; plus cross-file splices and run duplication.
* **C -- invariants, not just survival.** Byte-level mutation of the raw files,
  any of the 256 byte values, then every iteration checks all of:
  `enc.encode(enc.decode(raw)) == raw`; `tokenize(text).text == text`;
  `cst.parse` and `sema.analyse` do not raise; `doc.source` is unchanged after
  `run_local_layers`; `Document.open` then `save_source` on the mutated bytes is
  byte-identical; a structured edit through `patch.replace_node` changes nothing
  outside its own span; `undo()` restores the previous text exactly.

All three were run twice, once against the state of `core/lang/` before the
fix described in 5.2 and once after; the table gives the second run.

| campaign | seeds | iterations | crashes | hangs (>=10 s) | lexer lost text | document mutated its source | other invariant failures | median / p99 / max per iteration | wall |
|---|---|---:|---:|---:|---:|---:|---:|---|---:|
| A | 20260828, 1, 42, 31337, 987654321 | 25 000 | 0 | 0 | 0 | 0 | n/a | 1.7 / 5.5 / 10.8 ms | 54 s |
| B | 7, 13, 2024, 555555, 8675309 | 25 000 | 0 | 0 | 0 | 0 | n/a | 3.8 / 14.3 / 25.1 ms | 110 s |
| C | 101, 202, 303, 404, 505 | 25 000 | 0 | 0 | 0 | 0 | 0 | 12.7 / 25.9 / 44.6 ms | 341 s |

**75 000 iterations, zero crashes, zero hangs, zero cases where the lexer lost
text, zero cases where a document mutated its own source, and in campaign C zero
failures of the encoding inverse, the save round trip, the span-locality of a
structured edit, or undo.** The slowest single iteration across all three
campaigns was 44.6 ms, against a 10 s hang threshold. The first run, against
the earlier state, gave the same zero counts with a higher tail (campaign A p99
9.1 ms rather than 5.5 ms -- `Node.walk` became iterative in between).

The plain reading of that result: mutating a corpus of small, well-formed files
finds nothing, because the failures this front end actually has are size
failures, and 1 to 120 edits to a 2 kB fixture never produce a 200 kB file with
a 10 000-term expression. 5.2 is what happens when you go looking for those
directly.


### 5.2 A crash the fuzzer does not reach, and the crash its fix introduced

Random mutation of the corpus cannot produce this one: every file in the corpus
has short expressions. It turned up while building a large synthetic for
section 3, and it moved twice while this report was being written, because
another agent fixed it in the middle. Both states were measured. The digests
are named so the two halves can be told apart.

**Before** -- `cst.py` `3f9a1ceb`, `sema.py` `76883d4a`.

A flat sum of about 500 terms made `Document.run_local_layers()` raise
`RecursionError`:

```python
head = 'declare_output(name = "o", first = 0.0, interval = 1.0);\n'
n = 497
text = (head + "".join("w%d = 1.0;\n" % i for i in range(n))
        + "v = " + " + ".join("w%d" % i for i in range(n)) + ";\nv;\n")
Document.from_text(text).run_local_layers()
# RecursionError: maximum recursion depth exceeded while calling a Python object
```

10 172 bytes, 500 lines, entirely ordinary syntax. Bisected on that state, with
`sys.getrecursionlimit() == 1000`:

| entry point | largest flat sum that survived | first that raised | where |
|---|---:|---:|---|
| `cst.parse` | n/a | never; iterative | n/a |
| `ParseResult.statements()` | n/a | never; iterative | n/a |
| `Node.walk()` | 995 | 996 | `cst.py`, `walk()` |
| `sema.analyse` | 497 | 498 | `sema.py`, `type_of()` |
| `Document.run_local_layers` | 496 | 497 | `sema.py`, `type_of()` |

The cause was that `Limits.max_ast_depth = 256` is enforced against *parser
recursion*, not against the depth of the tree the parser produces.
`_Parser._expr` increments `self.depth` and refuses past `max_depth`, which
catches every shape whose depth comes from nesting; a left-associative chain is
consumed by a `while` loop inside one `_expr`, so `self.depth` never rose above
1 while the tree came out as deep as the sum was long. `Node.walk` and
`sema._type_of` then walked it recursively with no guard of their own.

The guard did work for every shape it was designed for. Measured on that same
state, only the flat chain slipped through:

| shape | bytes | CST depth | parse problems | opaque spans | `run_local_layers` |
|---|---:|---:|---|---:|---|
| flat sum, 400 terms | 7 443 | 401 | none | 0 | ok |
| flat sum, 600 terms | 11 243 | 601 | none | 0 | **RecursionError** |
| flat sum, 5 000 terms | 102 843 | n/a | none | n/a | **RecursionError**, in `walk()` |
| 200 nested parens (`deep_nesting.pot`) | 476 | 202 | none | 0 | ok |
| 300 nested parens | 676 | 3 | `SYN010` | 1 | ok |
| 5 000 nested parens | 10 076 | 3 | `SYN010` | 1 | ok |
| 400 nested `norm()` calls | 2 476 | 3 | `SYN010` | 1 | ok |
| 400-deep `if/then/else` chain | 7 678 | 3 | `SYN010` | 1 | ok |
| 600 unary minus signs | 676 | 3 | `SYN001` | 1 | ok |

Nothing was lost or corrupted: the source text was never touched, `save_source`
still worked, and the GUI's `AnalysisWorker.run` caught it
(`RecursionError` is an `Exception`) and showed
`syntax layer: error | analysis failed: maximum recursion depth exceeded` with
`doc.source` intact. `potcheck.main` calls `run_local_layers()` unguarded and
printed a traceback to stderr with exit code 1, which is the same exit code it
uses for a clean rejection.

**After** -- `cst.py` `1c2c09e3`, `sema.py` `6dad4547`.

The fix that landed does three things: `Node.walk` is now iterative with its own
stack; `Node.tree_depth()` measures the real tree depth iteratively; and
`sema.analyse` measures that depth, refuses past a new
`sema.MAX_TREE_DEPTH = 20_000` with a `SYN010` warning, and otherwise raises
CPython's recursion limit to `1000 + depth * 8` for the duration.
`tests/test_lang.py` gained a regression test for it.

Re-running the same probes against the fixed state: every row above that said
**RecursionError** now says ok, and every `SYN010` row is unchanged. The 5 000
term chain now parses to depth 5 001 and analyses cleanly.

**But the fix introduced a segmentation fault.** `1000 + depth * 8` is a
recursion limit the C stack cannot honour. `_type_of` and `type_of` cost two
Python frames per level, and on this machine's default 8 MiB stack
(`ulimit -s` = 8192) the process dies before it reaches the new ceiling:

| terms | bytes | recursion limit `sema.analyse` sets | outcome | wall |
|---:|---:|---:|---|---:|
| 8 200 | 170 043 | 66 616 | survives | 27.1 s |
| 9 000 | 186 843 | 73 016 | survives | 33.3 s |
| 9 500 | 197 343 | 77 016 | survives | 37.2 s |
| 9 750 | 202 593 | 79 016 | **SIGSEGV, exit 139** | 0.38 s |
| 10 000 | 207 843 | 81 016 | **SIGSEGV, exit 139** | 0.42 s |
| 12 000 | 253 843 | 97 016 | **SIGSEGV, exit 139** | 0.44 s |
| 16 000 | 345 843 | 129 016 | **SIGSEGV, exit 139** | 0.45 s |
| 20 050 | 438 993 | -- | refused with `SYN010`, source intact | 0.52 s |

`faulthandler` names the frame:

```
Fatal Python error: Segmentation fault

Current thread 0x00007893177b2740 (most recent call first):
  File ".../funnelforge/core/lang/sema.py", line 547 in _type_of
  File ".../funnelforge/core/lang/sema.py", line 541 in type_of
  File ".../funnelforge/core/lang/sema.py", line 563 in _type_of
  File ".../funnelforge/core/lang/sema.py", line 541 in type_of
  ... repeating ...
```

It is the C stack and nothing else: the same 9 750-term file that segfaults
under the default 8 MiB stack survives under `ulimit -s 65536`, with
`survived: codes=['MTD001', 'SEM004']`. So the threshold is not a property of
the code alone -- it moves with the user's `ulimit`, which means the same file
will crash on one workstation and analyse on the next.

**Why this is worse than what it replaced.** A `RecursionError` is an
`Exception`, so `AnalysisWorker.run` catches it, the window stays up and the
buffer survives. `SIGSEGV` kills the whole process from a worker thread; the
GUI cannot catch it and whatever the user had not saved is gone. The bad range
is roughly 9 500 to 20 000 terms -- a 200 kB to 440 kB file -- which is inside
`Limits.max_file_bytes` (32 MiB) and inside `MAX_TREE_DEPTH` (20 000), so
nothing else refuses it first. Above 20 000 terms `MAX_TREE_DEPTH` catches it
and the behaviour is correct again; the hole is entirely below the ceiling that
was added.

**Residual: analysing a long chain is quadratic, for the reason in 3.2b.**
Independent of the crash, and measured on the fixed state:

| terms | bytes | tree depth | parse | `sema.analyse` | `run_local_layers` |
|---:|---:|---:|---:|---:|---:|
| 500 | 9 343 | 502 | 11 ms | 122 ms | 126 ms |
| 1 000 | 18 843 | 1 002 | 24 ms | 424 ms | 437 ms |
| 2 000 | 39 843 | 2 002 | 39 ms | 1 594 ms | 1 684 ms |
| 4 000 | 81 843 | 4 002 | 95 ms | 6 301 ms | 6 666 ms |
| 8 000 | 165 843 | 8 002 | 188 ms | 25 928 ms | 26 816 ms |

Parse is linear; `analyse` quadruples for every doubling. A 166 kB file that
parses in 188 ms takes 26 s to analyse. It is the same `opaque_nodes` property
as 3.2b -- an N-term sum is N contributions, and each one re-walks the program.
Memoising that property on the same files:

| terms | `analyse` as shipped | with `opaque_nodes` memoised |
|---:|---:|---:|
| 500 | 137 ms | 8 ms |
| 1 000 | 420 ms | 17 ms |
| 2 000 | 1 639 ms | 34 ms |
| 4 000 | 6 386 ms | 68 ms |
| 8 000 | 26 174 ms | 154 ms |

Linear once the property is cached, and 170x faster at 8 000 terms. Worth
noting for the segfault above: with the analysis this much faster, the deep
range is reached in a fraction of a second rather than half a minute, so the
crash arrives sooner rather than going away.

**Suggested fix.** Making `_type_of` iterative, the way `walk` and `tree_depth`
now are, removes all three problems at once: the segfault, the dependence on
`ulimit`, and the need to touch `sys.setrecursionlimit` at all. Failing that,
`MAX_TREE_DEPTH` has to be set from what the C stack can actually hold -- under
4 000 for a two-frame-per-level walk on an 8 MiB stack, with margin -- rather
than from a number the recursion limit will accept.

The reproducer scripts and every fuzz case saved during these runs are in the
scratchpad under
`/tmp/claude-1000/-home-bugra-Claude-Code/83b73292-bc21-4bee-8621-8ece6a3f707c/scratchpad/`;
`segv.py <n>` is the one-argument reproducer for the segmentation fault.
