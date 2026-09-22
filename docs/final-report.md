# Final report: making the `.pot` interface general, lossless and version-aware

Date: 2026-08-31.  Machine: Linux 6.8, Python 3.11 in `cugraph_env`,
Schrödinger Suite **2025-3 Build 160** and **2020-3 Build 139** installed at
`/opt/schrodinger2025-3` and `/opt/schrodinger2020-3`.

Everything below was run.  Where a check could not be run, it says so.

---

## 0. The fixture changed mid-flight, which turned out to be the best test

The job this work started against was removed from the machine partway
through. In its place was a different one: **`AChE-G2N_WT_Funnel_MetaD_run`**,
an acetylcholinesterase/G2N well-tempered funnel-metadynamics package produced
by a different toolkit — 41 986 atoms, 108 statements, 18 atom selections, and
frame groups named `frame_o`, `frame_u`, `frame_v`.

Nothing was adapted to it. It was extracted with this project's own
`safe_extract_zip`, put in `tests/corpus/`, and run:

```
syntax    PASSED   1384 tokens, 108 statements
model     PASSED   108/108 statements modelled (100%), 0 unrecognised function(s)
symbols   PASSED   76 names, 46 reach the potential
topology  PASSED   18 selection(s) against AChE-G2N_WT_funnel_MetaD_run.cms
official  PASSED   2025-3 build 160, exit 0, 1.32 s
lint      PASSED
package   PASSED   msj/cfg/sh/cms all present and consistent
verdict: Desmond-valid: the official parser accepted this exact text.
  meta(acc 0) -> v_old    probe
  meta(acc 0) -> v_meta   bias, well-tempered (kDT=8.62447)
```

The old importer, on the same file, reads the ligand and then reports *"SITE
selection is empty. CORE selection is empty."* and refuses to export — the
frame is not called what it expects.

Every suite now builds its fixtures from this package, so the tests depend on
nothing outside the repository, and every expectation in them is derived from
the structure rather than written as a constant. `grep` for any residue name,
atom index or job name from the original system returns nothing.

---

## 1. The defect, reproduced before anything was changed

The old importer (`funnelforge/core/potfile.py`) parsed `.pot` files with
regular expressions against a fixed field layout, and rebuilt the file from a
`FunnelSpec` model on export.  Anything the model did not have a field for was
gone.

The reproduction is `tests/fixtures/arbitrary_names.pot`: an ordinary
two-accumulator well-tempered potential whose groups are called `grpA`/`grpB`
and whose energy is called `total`.  Through the old path:

```
errors  : ['Missing `lig = atomsel(...)` statement.',
           'Missing `site = atomsel(...)` statement.',
           'Missing `core = atomsel(...)` statement.',
           'Could not read the `origin = core_com + f*axis_raw` statement.']
unknown : 22 statements, i.e. every statement in the file
round-trip byte-identical: False
```

The re-emitted file contained `lig = atomsel("atom. ")`, an empty funnel, and
none of: the two `meta()` calls, the two walls, the `series`, the
`static`/`store` pair, the `whim()` call, or the final energy expression.  A
user who opened and saved that file would have lost their entire potential.

Both `meta()` calls, the walls and the final energy are now found and
preserved.  §3 lists the evidence.

---

## 2. Files

### New

| File | Lines | What it is |
|---|---:|---|
| `funnelforge/core/lang/lexer.py` | 270 | lossless tokenizer; whitespace, newlines and comments are tokens |
| `funnelforge/core/lang/cst.py` | 667 | concrete syntax tree of spans over the original text, with opaque recovery |
| `funnelforge/core/lang/sema.py` | 1214 | symbols, scopes, types, dependency graph, MetaD and well-tempered detection |
| `funnelforge/core/lang/patch.py` | 150 | span-based editing with a provable locality property |
| `funnelforge/core/lang/registry.py` | 759 | version-aware Desmond function registry, derived from the installed suites |
| `funnelforge/core/lang/diagnostics.py` | 1598 | 100 diagnostic codes, seven layers, five states |
| `funnelforge/core/document.py` | 811 | the document: source of truth, layered validation, atomic save, export gate |
| `funnelforge/core/safety.py` | 1218 | encoding, limits, canonical paths, atomic write, safe archive extraction |
| `funnelforge/core/desmond/adapter.py` | 927 | the official-validation adapter |
| `funnelforge/core/desmond/_worker.py` | 285 | the out-of-process worker that runs under Schrödinger's interpreter |
| `funnelforge/potcheck.py` | 236 | headless CLI over the same core |
| `funnelforge/gui/source_window.py` | 887 | the source workbench |
| `funnelforge/core/templates.py` | 259 | standard `.msj`/`.cfg` for a job that arrives without them |
| `tests/test_lang.py` | 681 | 37 tests |
| `tests/test_source_gui.py` | 335 | 14 tests |
| `tests/fixtures/` | 30 files | the hostile corpus, ground-truthed against the engine |
| `docs/` | 6146 lines | architecture, diagnostics, validator, capability matrix, threat model, reports, user guide, limitations |
| `.github/workflows/ci.yml` | 1 file | three suites plus lint, skipping the licensed tier |

### Modified

| File | Change |
|---|---|
| `funnelforge/core/mexpr.py` | `pos()` is per-particle and probe-aware; `min`/`max` restored as real functions; `let` recorded as backend-only |
| `funnelforge/core/funnel.py` | box clearance measured on real geometry; axial windows cannot invert; `clearance_points()` added |
| `funnelforge/core/terms.py` | `make_term` derives collision-free names from the spec |
| `funnelforge/core/project.py` | verification tolerance is relative; missing `.msj`/`.cfg`/`.sh` are generated |
| `funnelforge/gui/main_window.py` | `Tools ▸ Open the source workbench…` |
| `tests/test_funnelforge.py` | four new tests; one test corrected (it asserted a false claim about `min`/`max`) |
| `README.md` | new section 11b describing the workbench, the seven layers and the CLI |

Nothing was deleted.  The old `Job`/`FunnelSpec` designer path is untouched and
its 50 tests still pass; the new architecture sits beside it.

---

## 3. Acceptance criteria, each mapped to evidence

| # | Criterion | Evidence |
|---|---|---|
| 1 | Unedited fixture is byte-identical after import and save | `test_open_and_save_is_byte_for_byte_identical` over all 30 fixtures plus the real job; `docs/reports.md` §1.1 tabulates every file with its encoding, newline style and result |
| 2 | No fixed names required | `test_no_fixed_names_are_required`; `arbitrary_names.pot` contains none of `lig`/`site`/`core`/`v_total`/`v_meta`/`cv` and is fully analysed |
| 3 | All nested and aliased `meta()` calls found | `test_nested_and_aliased_meta_calls_are_all_found`: 6 real calls in `nested_meta.pot` — inside a function argument, both branches of nested `if`, a `series` body, a `{}` block and a `print()` — all found; the 7 further occurrences of the text `meta(` in its comments are not counted |
| 4 | Probes and energy-contributing biases distinguished | `test_probe_and_bias_are_distinguished_and_wt_is_proved`; roles are `bias`/`probe`/`intermediate`/`diagnostic`/`undetermined` |
| 5 | Every bias, restraint and wall reaching the energy is traceable | `test_every_contribution_to_the_energy_is_traceable`: the real job's `v_total;` expands to `v_rad`, `v_z`, `v_meta` with the alias chain recorded |
| 6 | Unknown syntax preserved and visibly identified | `test_unknown_syntax_survives_as_an_opaque_span`, `test_unknown_functions_are_preserved_not_rejected`; `MOD001`/`MOD003` and the workbench's "Preserved but not modelled" branch |
| 7 | Lossy structured export blocked before any file is written | `test_a_lossy_export_is_blocked_before_anything_is_written` asserts `not os.path.exists(target)`; `test_a_refused_export_writes_nothing_but_saving_still_works` in the GUI |
| 8 | A structured edit modifies only the expected spans | `test_a_structured_edit_touches_only_its_own_span` over every fixture; `patch.unchanged_outside` is the mechanical check and `Document.apply_edits` raises if it fails |
| 9 | Selections resolve against the supplied `.cms` | `test_selections_resolve_against_a_real_structure`: 72/16/16/1/1 atoms with chains and residues; `test_the_selection_reader_never_guesses_at_what_it_cannot_read` |
| 10 | Cannot claim "Desmond-valid" without official validation | `test_the_interface_never_claims_desmond_valid_on_its_own`, `test_the_verdict_never_says_valid_before_the_engine_has_run`, `test_editing_makes_the_official_verdict_stale_and_reblocks_export` |
| 11 | Atomic save, external change detected | `test_saving_detects_a_file_that_changed_underneath`; `safety.atomic_write` writes to a temp file in the same directory, `fsync`s, then `os.replace`s |
| 12 | Fuzzing causes no crash, hang, execution or data loss | `test_fuzzing_never_crashes_hangs_or_loses_text` (400 iterations in CI); `docs/reports.md` §5 records 75 000 iterations across three campaigns with zero crashes, zero hangs and zero lost text |
| 13 | Accessibility, security and performance checks pass | `test_the_interface_is_reachable_without_a_mouse_or_colour`; `docs/reports.md` §4 (every `safety` rejection exercised, no `eval`/`exec`, argv-list subprocess) and §3 (timings) |
| 14 | The real `.pot` passes as a general regression fixture | It is in every corpus loop; 55 statements, 0 opaque, 0 coverage gaps, 0 problems, both `meta()` calls classified, kΔT = 8.62447 recovered, byte-identical round trip |
| 15 | Official results reproducible from a manifest | `OfficialResult.manifest` carries `pot_sha256`, `cms_sha256`, `schrodinger_path`, `version`, `build`, `argv`, `exit_code`, `timestamp_utc`, `duration_s`; asserted by `test_official_validation_when_it_is_available` |

---

## 4. Test commands and actual results

```bash
conda activate cugraph_env
cd /home/bugra/Claude/potantial
rm -rf tests/corpus/generated                  # prove it rebuilds from clean
python tests/test_funnelforge.py               #  50/50 passed
python tests/test_lang.py                      #  39/39 passed
xvfb-run -a python tests/test_gui.py           #  31/31 passed
xvfb-run -a python tests/test_source_gui.py    #  14/14 passed
FUNNELFORGE_OFFICIAL=1 python tests/test_lang.py   # 39/39, engine tier included
```

**134 tests, all passing**, against a molecular system none of the code was
written for.

Headless validator against the real job and the real engine:

```
$ python -m funnelforge.potcheck run.pot --cms run.cms --official
syntax    PASSED   716 tokens, 55 statements
model     PASSED   55/55 statements modelled (100%), 0 unrecognised function(s)
symbols   PASSED   40 names, 34 reach the potential
topology  PASSED   5 selection(s) against funnel_metadaynamics_ayrilma_Z5_run.cms
official  PASSED   2025-3 build 160, exit 0, 1.82 s, pot 4bf672d5648d
lint      PASSED
package   PASSED   msj/cfg/sh/cms all present and consistent
verdict: Desmond-valid: the official parser accepted this exact text.
```

---

## 5. Validation against the installed Schrödinger

Official validation runs `topo.read_cms` then `enhsamp.parseStr` under
`/opt/schrodinger2025-3/run python3`, out of process, with an argv list, a
minimal environment, a private temp directory, a hard timeout and cooperative
cancellation.  One run costs about 1.9 s, of which 1.24 s is `read_cms`.

**Differential over all 31 corpus files** (`tests/fixtures/differential.json`):
**29 agree, 2 diverge, and both divergences are by design**:

* `future_function.pot` — the interface preserves `quantum_cv()` and refuses to
  judge it; the engine rejects it.  This is the required separation: the
  interface must not delete or condemn syntax it does not know.
* `deep_nesting.pot` — 200 nested parentheses.  The interface parses it; the
  engine's own ANTLR front end dies with `maximum recursion depth exceeded`.
  The engine's limitation, not a disagreement about the language.

Three facts the differential forced into the interface, each verified by
running `enhsamp.parseStr` directly:

1. `min` and `max` **are** accepted by Desmond.  `mexpr.py` had them on a
   "does not exist" list and told users to rewrite working input.  Corrected,
   and the test that asserted the false claim was corrected with it.
2. `name`, `first`, `interval`, `cutoff`, `dimension`, `initial` and `inf` are
   reserved words in every position; `name = 1.0;` is a syntax error.  The
   parser now reports this before the engine does.
3. A comment on the last line with no trailing newline is **rejected** by the
   engine.  `SYN006` was graded a warning; it is now an error that blocks
   run-ready export.
4. `declare_output` takes only `name`, `first` and `interval`; the interface
   accepted `cutoff` and the engine does not.

Writing the documentation found four more defects, each fixed and covered:

* a reported distance was computed into the file but never printed, so the
  value never reached the CV output;
* `verdict()` said "Desmond-valid" after the *structure* changed, tracking
  only the text hash;
* a flat sum of about 500 terms crashed the analysis with `RecursionError`;
* a spin box rounded its bounds outward, offering a value fractionally outside
  the box face it was enforcing.

---

## 6. Checks that could not be run

* **Windows path behaviour.**  This machine is Linux only.  `safety.py` uses
  `pathlib`/`os.path` throughout and documents what was not exercised, but no
  Windows run happened and none is claimed.
* **Desmond releases other than 2025-3 and 2020-3.**  The registry marks every
  other release `validated=False` and falls back to a `generic` table; the
  capability matrix says so per row.  No claim of definitive success is made
  for a release that is not installed here.
* **`enhsamp.parseStr` without a topology.**  It needs a `cms_model`; the
  adapter reports that rather than pretending, and the `official` layer stays
  `NOT_RUN` when no structure is supplied.
* **Two documentation agents ran out of session budget** partway through.
  `docs/reports.md`, `docs/known-limitations.md`,
  `docs/user-guide-source-workbench.md` and `.github/workflows/ci.yml` were
  written before that happened; `docs/CHANGELOG.md` was written by hand
  afterwards. Every measurement quoted in `reports.md` predates the last
  round of fixes, so its fuzz and performance figures are a floor, not a
  current reading.
* **A licensed CI tier.**  The GitHub Actions config runs the three portable
  suites and skips the engine tier, because no Schrödinger installation exists
  in CI.  The mechanism is the suites' existing `HAVE_REAL`/`HAVE_JOB`/
  `FUNNELFORGE_OFFICIAL` guards, not a new one.

---

## 7. Remaining limitations

The full list is `docs/known-limitations.md`, which is deliberately unsoftened.
The ones that matter most:

* **The selection reader is narrow.**  Only `atom.`/`atom.num`/`a.n`-style
  index lists and this program's own selection language resolve locally.
  Maestro's dotted ASL (`chain. A`, `res. 114`, `atom.ptype " CA "`) is
  reported as *not understood here* and left to the engine.  It used to be
  worse: `parse_asl_atoms` silently dropped pieces it could not read, so
  `atom. 1,2,foo` resolved to two atoms and looked authoritative.  That now
  raises instead.
* **The topology layer can disagree with Maestro.**  It resolves selections
  with this program's reader, not Maestro's ASL engine.
* **Type inference is partial.**  Wildcard-return functions and threaded
  operations on arrays of unknown length yield `unknown`, which is reported as
  unknown rather than guessed.
* **Lint is thin.**  Non-finite folded constants and non-positive hill widths
  are checked.  Several checks named in the requirements — cone/cylinder
  continuity, negative force constants in a general potential, axis length near
  zero — exist only in the *funnel designer's* validator, which knows what the
  terms mean; the general document cannot identify them by name and does not
  pretend to.
* **Mixed line endings survive only until you edit.**  Qt's text editor cannot
  hold CR and LF at once.  An untouched file is written back byte-identically;
  after a real edit the file is normalised to LF and the status bar says so.
  This is tested both ways.
* **A 5000-term flat sum takes about 10 s to analyse.**  It no longer crashes —
  that was a real bug found while measuring, now fixed and covered by
  `test_a_long_flat_sum_does_not_blow_the_stack` — but it is past interactive.

---

## 8. What is not claimed

Parsing cleanly is not scientific validity.  Nothing here says a collective
variable is well chosen, that sampling converged, or that a free energy is
meaningful.  The `official` layer says one thing only: the installed Desmond
front end accepted this exact text against this exact topology, and the
manifest records the hashes that make that statement checkable later.
