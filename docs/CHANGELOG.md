# Changelog

## 2026-08-31 — a general, lossless `.pot` source editor and validation system

### The problem this release exists to fix

The `.pot` importer parsed with regular expressions against a fixed field
layout and rebuilt the file from a `FunnelSpec` on export. Anything the model
had no field for was silently dropped, and three variable names — `lig`,
`site`, `core` — were mandatory.

Reproduced on `tests/fixtures/arbitrary_names.pot`: four errors, all 22
statements filed as unknown, and an export that produced a *different
potential* — empty selections, both walls gone, the `series` collective
variable gone, the `static`/`store` pair gone, and the user's own well-tempered
constants replaced by this project's defaults.

Reproduced again, on a real production package this time. The AChE/G2N job in
`tests/corpus/` names its frame groups `frame_o`, `frame_u`, `frame_v`. The old
importer reads its ligand and then reports *"SITE selection is empty. CORE
selection is empty."* and refuses to export. The new path reads all 108
statements, resolves all 18 atom selections against the 41 986-atom structure,
classifies both `meta()` calls, recovers the well-tempered factor, and
round-trips the file byte for byte.

### Added

**A lossless front end for the language** (`funnelforge/core/lang/`)

* `lexer.py` — every character, including whitespace, line endings and
  comments, belongs to exactly one token. `"".join(t.text for t in tokens)`
  reproduces the file.
* `cst.py` — a tree of spans over the original text. Nothing is copied or
  normalised. Anything the grammar cannot explain becomes an `OPAQUE` node
  that keeps its exact span, so a future Desmond release can add syntax and
  the file still opens, displays and saves without loss. `coverage_gaps()` is
  the machine-checkable form of "nothing was dropped".
* `sema.py` — symbols, scopes, an array-of-germs type model, a transitive
  dependency graph, reachability from the potential *and* from side-effecting
  calls, `meta()` discovery at any nesting depth, and well-tempered detection
  by data flow rather than pattern matching.
* `patch.py` — span edits with `unchanged_outside()`, the property that makes
  "a structured edit touches only its own span" checkable rather than hoped
  for.
* `registry.py` — the Desmond function table per release, derived from the
  installed suites, with a `generic` fallback that never rejects a function
  for lack of a version.
* `diagnostics.py` — 100 codes across seven independent layers and five
  states, keeping *unsupported*, *invalid*, *unused* and *unresolved* apart.

**The document and its two exits** (`funnelforge/core/document.py`)

* The source text is the single source of truth; every derived result is
  tagged with the revision it describes and goes `STALE` when the text moves.
* `save_source()` writes the bytes as they are, atomically, refusing to
  clobber a file that changed on disk. Always available.
* `export_run_ready()` refuses — before writing anything — when an unmodelled
  construct feeds the potential, or when the official parser has not accepted
  this exact text and this exact structure.

**Official validation** (`funnelforge/core/desmond/`) — the only component
allowed to establish Desmond compatibility. Runs the installed
`topo.read_cms` + `enhsamp.parseStr` out of process with an argv list, a
minimal environment, a private temp directory, a hard timeout and cancellation,
and records a manifest (both file hashes, version, build, argv, exit code,
timestamp, duration) that makes a run reproducible.

**Safety** (`funnelforge/core/safety.py`) — encoding detection that inverts
exactly, resource limits, canonical paths, atomic writes, and archive
extraction that refuses traversal, absolute members, symlinks, oversized
members and compression bombs.

**Interfaces** — the source workbench (`funnelforge/gui/source_window.py`,
reachable from *Tools ▸ Open the source workbench…*) with synchronised raw and
structured views, diagnostics that navigate to their exact span, cancellable
background workers whose stale results are discarded, and a diff before
saving; plus the headless `python -m funnelforge.potcheck`, which runs the same
core so CI and the screen cannot disagree.

**Tests** — `tests/test_lang.py` (39) and `tests/test_source_gui.py` (14),
a 30-file hostile fixture corpus, and a real production package in
`tests/corpus/` that the suites build their fixtures from, so nothing depends
on a path outside the repository.

### Fixed

* **`min` and `max` were listed as not existing in Desmond.** They do exist —
  verified against `enhsamp.parseStr` on both installed suites — and the
  interface was telling users to rewrite working input. `let` is the real
  backend-only name and is now listed instead.
* **A reported distance was computed but never printed.** `make_diagnostic`
  built the diagnostic while a separate list drove the `print()` lines, so a
  diagnostic added by anything but the panel produced arithmetic in the file
  and no value in the CV output. The emitter now prints every diagnostic.
* **`pos()` was not probe-aware** and silently returned the first atom of a
  multi-atom group. It now takes one particle, as Desmond does, and moves with
  the probed group.
* **Box clearance measured an inflated envelope**, so a shape placed off the
  funnel axis was reported as leaving the cell when it did not. Clearance and
  the box check now read the same real geometry.
* **Clamping could turn a shape's axial window inside out** (`z_hi <= z_lo`).
* **A long flat sum crashed the analysis.** `a + b + … + z` builds a tree as
  deep as it is long while the parser's own depth guard never rises above one;
  at 497 terms `run_local_layers()` raised `RecursionError`. Tree traversal is
  iterative now and the analysis takes headroom from the measured depth.
* **Verification used an absolute tolerance.** A wall reaching 10⁹ kcal/mol
  cannot agree to 10⁻⁶ in double precision; pure rounding was reported as a
  failed export.
* **A spin box rounded its bounds outward**, offering a value a hundredth of a
  millipoint outside the box face it was enforcing.
* **The verdict claimed "Desmond-valid" after the structure changed.** It now
  tracks the topology hash as well as the text.
* **`declare_output` accepted terms the grammar forbids** (`cutoff`,
  `dimension`, `initial`), which the engine rejects.
* Reserved words (`name`, `first`, `interval`, `cutoff`, `dimension`,
  `initial`, `inf`), declarations after the first statement, and a comment on
  the last line with no trailing newline are all reported before the engine
  is asked — each verified to be fatal by running the engine.
* A malformed atom list (`atom. 1,2,foo`) resolved to two atoms and looked
  authoritative; it is now refused.
* Export from a bare `.cms` produced no `.msj`/`.cfg`, so the "run-ready" set
  was not runnable. The standard Desmond protocol is generated when it is
  missing.

### Rollback

Everything is additive. The funnel designer's own path — `Job`, `FunnelSpec`,
`potfile.py`, the designer tabs — was not replaced and its 50 tests still pass.

To return to the previous behaviour:

1. Delete the new packages: `funnelforge/core/lang/`,
   `funnelforge/core/desmond/`, `funnelforge/core/document.py`,
   `funnelforge/core/safety.py`, `funnelforge/core/templates.py`,
   `funnelforge/potcheck.py`, `funnelforge/gui/source_window.py`,
   `tests/test_lang.py`, `tests/test_source_gui.py`, `tests/fixtures/`,
   `tests/corpus/`, `tests/fixture_paths.py`, `docs/*.md` from this release.
2. Revert the modified files: `funnelforge/core/mexpr.py`,
   `funnelforge/core/funnel.py`, `funnelforge/core/terms.py`,
   `funnelforge/core/project.py`, `funnelforge/core/potfile.py`,
   `funnelforge/gui/widgets.py`, `funnelforge/gui/main_window.py`,
   `tests/test_funnelforge.py`, `tests/test_gui.py`, `README.md`.
3. Note that reverting `mexpr.py` restores the false claim about `min`/`max`,
   and reverting `potfile.py` restores the missing `print()` for diagnostics.
   Neither is desirable; prefer reverting only what you must.

The one behaviour a caller might depend on that changed: `pos()` on a
multi-atom selection used to return the first atom's position and now raises,
matching Desmond. Nothing in this repository relied on the old behaviour.
