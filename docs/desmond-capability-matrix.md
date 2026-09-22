# Desmond capability matrix

What each Desmond release accepts, as `funnelforge/core/lang/registry.py`
models it, and what was checked against the live compiler on this machine.

The registry is a *description*, not a checker. It performs no parsing. It is
the table that `funnelforge/core/lang/sema.py` and the GUI consult so the editor
and the compiler agree about what the language contains. It is derived from
`getFcnSigs()` in the installed suites, not from the manual:

```
/opt/schrodinger2025-3/internal/lib/python3.11/site-packages/schrodinger/
    application/desmond/enhanced_sampling/FcnTypes.py
```

Every measurement below was taken on 2026-08-28 on this machine, against
`/opt/schrodinger2025-3` (Suite 2025-3, Build 160, Python 3.11.4) and
`/opt/schrodinger2020-3` (Suite 2020-3, Build 139, Python 3.6.2), using
`topo.read_cms` on
`/home/bugra/Downloads/funnel_metadaynamics_ayrilma_Z5_run.cms` (62,870 atoms).

---

## 1. The rows

Generated from `registry.capability_matrix()`.

| Release | Build | Validated | Functions | Source |
| --- | --- | --- | ---: | --- |
| `2025-3` | 160 | **yes** | 58 | `/opt/schrodinger2025-3/internal/lib/python3.11/site-packages/schrodinger/application/desmond/enhanced_sampling/FcnTypes.py` |
| `2020-3` | 139 | **yes** | 58 | `/opt/schrodinger2020-3/internal/lib/python3.6/site-packages/schrodinger/application/desmond/enhanced_sampling/FcnTypes.py` |
| `legacy` | — | no | 57 | `/opt/schrodinger2025-3/internal/lib/python3.11/site-packages/schrodinger/application/desmond/packages/enhanced_sampling/FcnTypes.py` |
| `generic` | — | no | 58 | union of the tables above |

`validated` is the field that governs what the interface may claim. It is
`True` only for a table read out of the signature table that a real
installation *on this machine* actually loads at compile time. It is `False`
for the historical table and for the generic union, both of which are informed
guesses about some other installation.

The `notes` each row carries, verbatim from `capability_matrix()`:

| Release | Note |
| --- | --- |
| `2025-3` | Read from the installed suite and exercised against its own compiler: 53 names in `getFcnSigs()`, plus the five forms `enhsamp.py` intercepts. `min`, `max` and `pow` are all present. |
| `2020-3` | Read from the installed suite. Its signature table and its `mexp.g` grammar are byte-for-byte identical to 2025-3, so the language is unchanged across the two. |
| `legacy` | The Python-2 era table, still shipped under `packages/` in both installed suites but imported by nothing. Marked unvalidated because it is not the table any installation here compiles against; it is kept as the evidence of what an older front end rejects, namely `pow`. |
| `generic` | Fallback for an unknown or absent Desmond installation. The signature table is the union of everything known, so nothing is rejected for lack of a version; `backend_only` is correspondingly the widest set, because `pow` may or may not be accepted by the front end actually installed. |

The `2020-3` note overstates its case; §2 shows exactly where.

`DEFAULT_RELEASE` is `2025-3`. `GENERIC_RELEASE` is `generic`, and an
unrecognised release id resolves to it rather than raising —
`registry.signatures("2099-1")` returns 58 names, not an error, because a
caller holding a version string this module has never heard of is exactly what
the fallback is for.

### Where the 58 comes from

| Kind | 2025-3 | 2020-3 | legacy | generic |
| --- | ---: | ---: | ---: | ---: |
| `simple` | 35 | 35 | 34 | 35 |
| `thread` | 8 | 8 | 8 | 8 |
| `binary_thread` | 7 | 7 | 7 | 7 |
| `special` | 8 | 8 | 8 | 8 |
| **total** | **58** | **58** | **57** | **58** |

`getFcnSigs()` in 2025-3 returns 53 entries. Dumped from the live table:

```
{'BinaryThreadSyntax': 7, 'ThreadSyntax': 8, 'SimpleSyntax': 35,
 'ArraySyntax': 1, 'MetaSyntax': 1, 'RMSDSyntax': 1}  total 53
```

The registry's 58 is those 53 plus five names that never reach `getFcnSigs()`
because `enhsamp.py` intercepts them first: `atomsel` (rewritten into a literal
`array` of gids by `resolve_atomsel`), `load` and `store` (typed against the
`static` declarations in `FcnCall.get_type`), and `declare_meta` /
`declare_output` (grammar forms, not calls). A validator that consulted only
`getFcnSigs()` would wrongly reject all five.

The 53 names, in full:

```
* + - / ^ acos angle angle_gid angle_gid_radians angle_radians array atan2
center_of_geometry center_of_mass contact_map cos cross delta dihedral
dihedral_gid dihedral_gid_radians dihedral_radians dist dot elem exp gibbs_max
gibbs_min helix length log mass max meta min min_image mod ncoordination norm
norm2 pos pos_inner_prod pow print rad_gyration rmsd rmsd_torsion sign sin sqrt
sum time whim
```

Five of those (`+ - * / ^`) are written as operators in the grammar's
`fcnCall : IDENT^ POPEN!` rule and can never be called by name, which the
registry records as `callable_=False` so completion lists do not offer them.

### Other per-release fields

| Field | 2025-3 | 2020-3 | legacy | generic |
| --- | --- | --- | --- | --- |
| `side_effecting` | `declare_meta declare_output meta print store` | same | same | same |
| `keywords` | `else if series static then` | same | same | same |
| `backend_only` | `let` | `let` | `let pow` | `let pow` |
| `only_here` | — | — | — | — |
| `missing_here` | — | — | `pow` | — |

`RESERVED_WORDS` is the union of three sets and is release-independent:

```
cutoff declare_meta declare_output dimension else first if inf initial
interval name series static then
```

### Mapping an installation to a row

```
/opt/schrodinger2025-3   installation_version=('2025-3', '160')  release_for_installation=2025-3
/opt/schrodinger2020-3   installation_version=('2020-3', '139')  release_for_installation=2020-3
/opt/schrodinger         installation_version=None               release_for_installation=None
/opt                     installation_version=None               release_for_installation=None
/nonexistent             installation_version=None               release_for_installation=None
```

`/opt/schrodinger` returns `None` rather than guessing: it is a licence-only
stub containing one `licenses` directory, with no `version.txt` and no `run`.

---

## 2. What actually differs between the releases installed here

The registry's own note on the `2020-3` row claims its signature table and
grammar are "byte-for-byte identical to 2025-3". **The grammar is; the file is
not.** Checked directly:

| File | 2025-3 | 2020-3 | Identical? |
| --- | --- | --- | --- |
| `enhanced_sampling/mexp.g` | `3ebd9361b9d12ee8ae943aa20ab229e8d0a44d50827a269ea9480b1fc9a04f70` | `3ebd9361…9a04f70` | **yes** |
| `enhanced_sampling/FcnTypes.py` | `c22d9e5e940636418f46d625ba6643819a0a71f7b5e83dfee52813bc3ac7a6c3` | `28c31207b82f06d967ebf97d453122903023fa96817858c5f4f7c41c1d44dedc` | no |

`diff` of the two `FcnTypes.py` files gives three hunks, all cosmetic: `class
Syntax(object):` became `class Syntax:`, and two `raise TypeError(...)` calls
were re-wrapped across lines. No signature, no arity and no type code changed.

The claim the note *should* make is about the table, and that one holds. Loading
`getFcnSigs()` under each suite's own interpreter and comparing name for name:

```
2025-3 n = 53 python 3.11.4
2020-3 n = 53 python 3.6.2
same name set: True
only 2025-3: []
only 2020-3: []
differing entries: []
```

Stronger still: `enhsamp.parseStr` on `tests/fixtures/funnel_general.pot`
against the same topology produces byte-identical output under both suites —
3261 characters, SHA-256
`89c8d090ea83cbd2941868d2a86779cdb5a6d0e0459ec4252136476b5e457d96`. Running the
adapter under 2020-3 accepts the file and reports the same `compiled_chars`,
with `OFF007` attached because 2020-3 is not in `TESTED_RELEASES`.

So: **there is no capability difference between the two installed releases.**
The five-year gap moved the interpreter (3.6.2 → 3.11.4) and reformatted one
file. The language did not move.

### The `legacy` table

`.../desmond/packages/enhanced_sampling/FcnTypes.py` — the Python-2 era copy,
still shipped under `packages/` in both suites. Loaded under 2025-3 and diffed
against the live table:

```
legacy n = 52
in 2025-3 not legacy: ['pow']
in legacy not 2025-3: []
DIFF rmsd  2025-3 arg_lengths [2, 4]   legacy arg_lengths [2, 3]
```

Exactly the two axes the registry models (`_build_table(include_pow=...,
legacy_arities=...)` and `_LEGACY_NARGS = {"rmsd": (2, 3)}`), and nothing else.

It is marked `validated=False` for a reason that is checkable: nothing imports
it. `enhsamp.py` imports from `.enhanced_sampling`, and a grep for
`packages.enhanced_sampling` across the whole 2025-3 `schrodinger` package
returns no hits. It is kept in the registry as evidence of what an older front
end rejects — namely `pow` — not as a description of anything that runs here.

---

## 3. Verified against the live compiler

Each fact below was produced by calling `enhsamp.parseStr(cms, text)` under
`/opt/schrodinger2025-3/run python3`, with `cms` from `topo.read_cms` on the Z5
topology, and with file descriptors 1 and 2 captured so ANTLR's output is
visible. Every source fragment is prefixed by:

```
declare_output(name = "probe.cvseq", first = 0.0, interval = 1.0);
```

### 3.1 `min` and `max` ARE accepted

The repository previously said otherwise. `funnelforge/core/mexpr.py`'s
docstring still does, at line 19:

> `abs`, `min`, `max` and `tan` deliberately do not exist here because they do
> not exist there either.

That is wrong for `min` and `max` — and `mexpr.py` contradicts its own docstring
at line 895, where it implements both. `SimpleSyntax('min', 1, [-1])` and its
`max` counterpart are in the 2020-3 and 2025-3 tables, and the compiler accepts
them:

| Source | Result |
| --- | --- |
| `min(array(1.0, 2.0, 3.0));` | **accepted**, 147 characters of backend configuration |
| `max(array(1.0, 2.0, 3.0));` | **accepted**, 147 characters |
| `min(1.0);` | **accepted**, 129 characters |
| `max(1.0);` | **accepted**, 129 characters |
| `g = atomsel("atom. 1-4");`<br>`min(array(norm(pos(g[0])), norm(pos(g[1]))));` | **accepted**, 233 characters |

Arity is one argument, enforced by the engine:

| Source | Result |
| --- | --- |
| `min();` | `TypeError: Number of arguments to min must be in the set [1]` |
| `min(1.0, 2.0);` | `TypeError: Number of arguments to min must be in the set [1]` |

The registry agrees: `known("min", "2025-3")` is `True`, `known("min",
"legacy")` is `True`, and `"min" in backend_only("2025-3")` is `False`. Telling
a chemist to rewrite `min(...)` into `gibbs_min(...)` on the strength of the old
claim would have been telling them to rewrite working input.

`gibbs_min` is a genuinely different function, also present:
`gibbs_min(300.0, array(1.0, 2.0));` is accepted (155 characters). It is the
differentiable softened minimum; `min` is the hard one.

### 3.2 `abs`, `tan` and `let` are NOT accepted

All three fail the same way, before Desmond is ever invoked: `env.sigs[name]`
misses and the lookup raises a bare `KeyError`.

| Source | Engine's response |
| --- | --- |
| `abs(1.0);` | `KeyError: 'abs'` |
| `tan(1.0);` | `KeyError: 'tan'` |
| `let(1.0);` | `KeyError: 'let'` |
| `let(1.0, 2.0);` | `KeyError: 'let'` |
| `frobnicate(1.0);` (control) | `KeyError: 'frobnicate'` |

`abs` and `tan` are simply absent from the language — nothing implements them
anywhere, and they are correctly absent from the registry
(`known("abs")` is `False`).

`let` is the interesting one, and it is why `Release.backend_only` exists. The
compiler *emits* `let` itself: any block that binds a name lowers to a `let`
form in the backend configuration. Its own output for
`g = atomsel("atom. 1,2,3"); norm(center_of_mass(g));`:

```
{type=enhanced_sampling gids=[0 1 2] sexp=[let [[$g [literal 0.0 1.0 2.0]]] [norm [center_of_mass $g]]] …}
```

(A file with no bindings gets no `let`: `sexp=[norm [pos 1.0]]`.)

So the backend evaluator must implement `let` — the compiler would not emit a
form the backend cannot read. The front end nevertheless has no signature for
it, so `let(...)` written by hand in a `.pot` cannot compile, however valid it
looks. That is the whole point of the set: never tell a user one of these is
fine.

**Gap:** `registry.backend_only()` is exported, documented and populated, and
**nothing in the repository calls it.** A grep for `backend_only` across every
`.py`, `.md` and `.json` in the tree matches only `registry.py` itself.
Consequently `let(1.0);` is reported by the interface as a generic
`MOD003` warning — `` `let()` is not in the 2025-3 function registry; it is
preserved unchanged and left for the official validator to judge `` — the same
message `abs()` and `tan()` get, with no hint that this one is known to be
impossible. The information is in the table; nothing reads it.

`pow` is the one name whose availability actually varies:
`pow(2.0, 3.0);` is accepted by 2025-3 (133 characters), and `pow` is absent
from the `legacy` table.

### 3.3 Seven reserved words cannot be used as variables

`name`, `first`, `interval`, `cutoff`, `dimension`, `initial` and `inf` are
declared as implicit literal tokens in `mexp.g`, inside `output_term` and
`meta_term`:

```
output_term :   'name'       EQ             STRING               -> ^(NAME          STRING)
              | 'first'      EQ op=SUBTROP? (v+='inf' | v+=LIT)  -> ^(FIRST    $op? $v+)
              | 'interval'   EQ             (v+='inf' | v+=LIT)  -> ^(INTERVAL      $v+);
```

In ANTLR3 an implicit literal token outranks `IDENT`, so these words are
reserved *everywhere*, not only inside a declaration. Each was tested as
`<word> = 1.0;` on line 2, followed by a use of the name:

| Source line 2 | Exception | ANTLR output |
| --- | --- | --- |
| `name = 1.0;` | `SystemExit(1)` | `line 2:0 no viable alternative at input 'name'` |
| `first = 1.0;` | `SystemExit(1)` | `line 2:0 no viable alternative at input 'first'` |
| `interval = 1.0;` | `SystemExit(1)` | `line 2:0 no viable alternative at input 'interval'` |
| `cutoff = 1.0;` | `SystemExit(1)` | `line 2:0 no viable alternative at input 'cutoff'` |
| `dimension = 1.0;` | `SystemExit(1)` | `line 2:0 no viable alternative at input 'dimension'` |
| `initial = 1.0;` | `SystemExit(1)` | `line 2:0 no viable alternative at input 'initial'` |
| `inf = 1.0;` | `SystemExit(1)` | `line 2:0 no viable alternative at input 'inf'` |
| `notreserved = 1.0;` (control) | — | **accepted**, 159 characters |

Every one of the seven is followed by `unable to parse m-expression` on the same
descriptor. These arrive as `SystemExit`, not as an exception; see
`docs/official-validation.md` §6.

The interface catches this before the engine does: `cutoff = 1.0;` produces
`SYN001` — `` `cutoff` is a reserved word of the M-expression grammar (it is a
declaration term), so it cannot… `` — an error, not a warning.

### 3.4 A comment on the last line with no trailing newline is rejected

`mexp.g` line 123 defines a comment as requiring a terminator:

```
WS	:	(' ' | '\t' | COMMENT | NEWLINE)  {$channel=HIDDEN} ;
fragment COMMENT 	:	'#' (~('\r' | '\n'))* NEWLINE;
```

A comment with no newline after it therefore never becomes a `COMMENT` token,
and the `#` reaches the parser.

| Source (ends exactly as shown) | Result |
| --- | --- |
| `norm(pos(1));`⏎`# a trailing comment with no newline` | **rejected**: `line 3:36 no viable alternative at character '<EOF>'` |
| `norm(pos(1));`⏎`# a trailing comment with a newline`⏎ | **accepted**, 136 characters |
| `norm(pos(1));` with no trailing newline and no comment | **accepted**, 136 characters |

The third row matters: it is not the missing final newline that is fatal, it is
the missing newline *after a comment*. The fixture
`tests/fixtures/no_final_newline.pot` is accepted by the engine for exactly this
reason.

The interface warns about it as `SYN006`, "Comment not ended by a newline" — a
warning, not an error, since the fix is one keystroke and the file is otherwise
fine.

### 3.5 A declaration after the first ordinary statement is rejected

`mexp.g` line 52 onwards:

```
prog	: 	header block EOF -> ^(PROG header block);
header  :       (d+=decl SEMI) * -> ^(HEADER $d*);
decl    :       decl_meta | decl_output | static;
```

The header is closed the moment the block begins. There is no way back into it.

| Source | Result |
| --- | --- |
| `x = 1.0;`⏎`declare_meta(dimension = 1, cutoff = 10.0, first = 0.0, interval = 1.0, name = "p.kerseq", initial = "");`⏎`x;` | **rejected**: `line 3:0 no viable alternative at input 'declare_meta'` |
| `declare_meta(…);`⏎`x = 1.0;`⏎`x;` | **accepted**, 254 characters |
| `x = 1.0;`⏎`static keeper(1);`⏎`x;` | **rejected**: `line 3:0 no viable alternative at input 'static'` |

`static` is a `decl` too, which is why the third row fails identically. This is
the sole defect in `tests/fixtures/arbitrary_names.pot`: line 29 puts
`static keeper(1);` in the middle of the block. Everything else in that file
parses and type-checks.

The interface catches this as `SYN001`: "declarations belong at the top of the
file: Desmond's grammar is 'header block', so this declaration…".

### 3.6 Summary of §3

| Claim under test | Verdict from the live compiler |
| --- | --- |
| `min` / `max` accepted | **accepted** — the old "backend-only" claim was wrong |
| `abs` accepted | rejected, `KeyError: 'abs'` |
| `tan` accepted | rejected, `KeyError: 'tan'` |
| `let` callable | rejected, `KeyError: 'let'`, although the compiler emits `let` itself |
| `pow` accepted (2025-3) | accepted; absent from the `legacy` table |
| `name first interval cutoff dimension initial inf` usable as variables | all seven rejected, `no viable alternative at input '<word>'` |
| trailing comment with no final newline | rejected, `no viable alternative at character '<EOF>'` |
| missing final newline alone | accepted |
| declaration after the first statement | rejected, `no viable alternative at input 'declare_meta'` / `'static'` |
| 2025-3 and 2020-3 differ in capability | they do not — identical 53-name table, identical grammar, byte-identical compiled output |

---

## 4. What is NOT known

1. **Only two releases were measured: 2025-3 build 160 and 2020-3 build 139.**
   Both are installed here. Nothing else was run, and nothing else can be.

2. **`legacy` and `generic` are marked `validated=False` and must stay that
   way.** `legacy` is a table no installation on this machine compiles against;
   `generic` is a union built so that an unknown installation does not cause
   spurious rejections. Neither describes an engine that was ever asked a
   question here.

3. **The interface must not claim definitive success for an unvalidated
   release.** The mechanism is `OFF007`, raised by
   `adapter._release_diagnostics` whenever `inst.version` is not in
   `TESTED_RELEASES` (currently `frozenset({"2025-3"})`). Its wording is the
   correct one: the engine's verdict still stands — it is the engine — but the
   way this interface *reads* its messages may be out of date. Confirmed by
   running the adapter under 2020-3: `OFF002` (accepted) and `OFF007` (untested
   release) are both filed.

   Note that `TESTED_RELEASES` is narrower than `validated` in the registry:
   2020-3 is a validated *table* but an untested *message parser*. The two
   fields answer different questions and are correctly independent.

4. **`registry.known(name)` returning `False` is a statement about the
   registry, never about the name.** A `.pot` may come from a newer suite or a
   site build with extra collective variables. The interface renders these as
   `MOD003` "preserved unchanged and left for the official validator to judge",
   a warning that does not block an export. `tests/fixtures/future_function.pot`
   is the regression case: the interface passes it, the engine rejects it with
   `unknown function 'quantum_cv'`, and the engine's answer is the one that
   counts.

5. **Not tested against any Desmond newer than 2025-3.** If a future release
   adds a collective variable, the registry will call it unknown and the
   official layer will accept it. That is the intended failure direction —
   under-claiming, never over-claiming.

6. **`backend_only` is unenforced.** The set is correct and verified, but no
   code reads it (§3.2). Until something does, a user who writes `let(...)` is
   told only that the name is unrecognised.

7. **The signature *shapes* were transcribed, not exhaustively re-derived.**
   The name set, the class of each entry, the arity sets and the `pow`/`rmsd`
   differences were all read out of the live `getFcnSigs()` and compared
   programmatically. The individual dimensionality codes in `registry._SIMPLE`
   (for example `ncoordination` as `(1, 1, 1, -1, -2)`) were transcribed by
   hand from `FcnTypes.py` and only spot-checked against the compiler here.
