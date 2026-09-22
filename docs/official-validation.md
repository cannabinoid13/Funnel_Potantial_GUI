# Official Desmond validation

`funnelforge/core/desmond/` is the only part of FunnelForge allowed to say that
a potential is Desmond-valid. Everything else — the CST in
`funnelforge/core/lang/cst.py`, the semantic analyser in `sema.py`, the
interpreter in `funnelforge/core/mexpr.py` — is an independent reimplementation
of the language and can be wrong in both directions. This document says what
the adapter does, what it refuses to do, and what it costs.

Everything below was measured on this machine on 2026-08-28 against
`/opt/schrodinger2025-3` (Suite 2025-3, Build 160) and
`/home/bugra/Downloads/funnel_metadaynamics_ayrilma_Z5_run.cms` (62,870 atoms).
Where a claim was not measured, it says so.

Files:

| Path | What it is |
| --- | --- |
| `/home/bugra/Claude/potantial/funnelforge/core/desmond/adapter.py` | Discovery, process control, message classification, the manifest |
| `/home/bugra/Claude/potantial/funnelforge/core/desmond/_worker.py` | The script run by the suite's own interpreter |
| `/home/bugra/Claude/potantial/funnelforge/core/document.py` | `Document.validate_official`, `official_is_current`, `export_blockers` |
| `/home/bugra/Claude/potantial/funnelforge/core/lang/diagnostics.py` | The `OFF001`–`OFF010` catalogue and `Report.verdict()` |

---

## 1. The contract

1. Only `adapter.validate()` may establish Desmond compatibility. No other
   layer's success is evidence of anything about Desmond.
2. Until it has run and accepted, the headline verdict must not contain the
   word "Desmond-valid".
3. `ran` and `ok` are separate. `ran=False` means no verdict was reached (no
   installation, no topology, killed, worker crashed); it must never be
   painted as "your potential is broken".
4. A verdict belongs to one source text and one topology. When either changes,
   the verdict no longer applies.

Points 1–3 hold in the code as written. Point 4 holds only partially — see
[§10](#10-known-gaps).

The mechanism behind point 1 is in `Report.add`
(`diagnostics.py:1130`): every other layer escalates its state when a
diagnostic is filed against it, but the `official` layer is exempt, so only the
process that actually ran the engine can move it off `NOT_RUN` via
`Report.mark`.

```python
if layer == "official" or status.state is State.STALE:
    return
```

---

## 2. The two verdicts

`Document.verdict()` returns one sentence: what the engine said, then what this
build found. These are the real strings, from
`tests/fixtures/funnel_general.pot`.

Before the adapter has run (only `run_local_layers()` was called):

```
Desmond validation not performed. This interface: 1 warning; not run: topology, package.
```

After `doc.validate_official('/home/bugra/Downloads/funnel_metadaynamics_ayrilma_Z5_run.cms')`:

```
Desmond-valid: the official parser accepted this exact text. This interface: 1 warning; not run: topology, package.
```

After one further keystroke (`doc.set_source(doc.source + "\n")`):

```
Desmond validation is out of date - it ran against different text or a different structure. This interface: 1 warning; not run: topology, package; out of date: syntax, model, symbols, lint.
```

The other two heads, both produced by real runs:

| Fixture | Verdict |
| --- | --- |
| `bad_accumulator.pot` | `Desmond rejected this file. This interface: 1 error, 1 warning; not run: topology, package.` |
| `comment_styles.pot` | `Desmond rejected this file. This interface: 1 warning; not run: topology, package.` |

The fifth head, `Desmond accepted this file, with warnings from the engine`,
requires `State.WARNING` on the official layer.
`Document.validate_official` only ever marks `PASSED`, `ERROR` or `NOT_RUN`, so
**this string is unreachable through `Document`**. The `OFF010` diagnostic that
records engine warnings is filed correctly and is visible in the diagnostic
list; it just does not change the headline. Not fixed here; stated so the
reader does not go looking for it.

The layer table behind the verdict, from
`python -m funnelforge.potcheck tests/fixtures/funnel_general.pot --cms … --official`:

```
syntax    PASSED   nothing to report                  2026-08-28T20:39:07Z  8224ab25dd0b25ae166f540a36775c0f1cf0474b9f2f2ac50cb2fa528bfe2f36
          note: 709 tokens, 48 statements
model     WARNING  1 warning                          2026-08-28T20:39:07Z  8224ab25dd0b25ae166f540a36775c0f1cf0474b9f2f2ac50cb2fa528bfe2f36
          note: 48/48 statements modelled (100%), 0 unrecognised function(s)
symbols   PASSED   nothing to report                  2026-08-28T20:39:07Z  8224ab25dd0b25ae166f540a36775c0f1cf0474b9f2f2ac50cb2fa528bfe2f36
          note: 37 names, 37 reach the potential
topology  PASSED   nothing to report                  2026-08-28T20:39:07Z  8224ab25dd0b25ae166f540a36775c0f1cf0474b9f2f2ac50cb2fa528bfe2f36
          note: 4 selection(s) against funnel_metadaynamics_ayrilma_Z5_run.cms
official  PASSED   1 info                             2026-08-28T20:39:09Z  8224ab25dd0b25ae166f540a36775c0f1cf0474b9f2f2ac50cb2fa528bfe2f36
          note: 2025-3 build 160, exit 0, 1.85 s, pot 8224ab25dd0b
lint      PASSED   nothing to report                  2026-08-28T20:39:07Z  8224ab25dd0b25ae166f540a36775c0f1cf0474b9f2f2ac50cb2fa528bfe2f36
package   NOT_RUN  nothing to report                  never                 -
```

### 2.1 The `OFF` codes

`adapter._REASON_CODE` maps the adapter's own failure vocabulary onto the
catalogue. The mapping is deliberately coarse, and deliberately conservative:
anything that stopped short of a verdict becomes "not performed" or "crashed",
never "rejected".

| Catalogue code | Severity | Blocks export | Adapter reasons that produce it |
| --- | --- | --- | --- |
| `OFF001` Official validation not performed | info | no | `topology-required`, `topology-unreadable`, `cancelled` |
| `OFF002` Accepted by Desmond | info | no | `accepted` |
| `OFF003` Rejected by Desmond | error | **yes** | `syntax`, `unknown-function`, `unknown-variable`, `arity`, `type`, `duplicate-binding`, `invalid-asl`, `engine-error` |
| `OFF004` Schrodinger installation not found | warning | no | `no-installation` |
| `OFF005` Official validation timed out | warning | no | `timed-out` |
| `OFF006` Official validator crashed | warning | no | `import-failed`, `no-result` |
| `OFF007` Untested Desmond release | warning | no | appended when `inst.version` is not in `TESTED_RELEASES` (`frozenset({"2025-3"})`) |
| `OFF008` Validation is out of date: text | warning | no | raised by `Document.export_blockers`, not by the adapter |
| `OFF009` Validation is out of date: system | warning | no | raised by `Document.export_blockers`, not by the adapter |
| `OFF010` Accepted, with warnings | warning | no | `warnings` (engine printed something and still accepted) |

`OFF003` is the only official code that blocks a run-ready export. `OFF001`
blocks nothing on its own; the export gate for "never validated" is a separate
`OFF001` diagnostic built by `Document.export_blockers`.

Real diagnostics from real runs:

| Situation | Code | `data["reason"]` | Message |
| --- | --- | --- | --- |
| `funnel_general.pot` accepted | `OFF002` | `accepted` | `accepted by the official Desmond parser (2025-3 build 160); it produced 3261 characters of backend configuration` |
| `bad_accumulator.pot` | `OFF003` | `engine-error` | `metadynamics accumulator id outsides range of accumulators` |
| `comment_styles.pot` | `OFF003` | `syntax` | `no viable alternative at character '<EOF>'` at line 42, col 80 |
| `validate(text, None)` | `OFF001` | `topology-required` | the `NO_TOPOLOGY_REASON` paragraph |
| `validate(text, '/tmp')` | `OFF001` | `topology-unreadable` | `topology is not a regular file: /tmp` |
| `installation=/opt/schrodinger` | `OFF004` | `no-installation` | `Schrodinger installation at /opt/schrodinger cannot be used: no 'run' wrapper in this directory` |
| `timeout=0.5` | `OFF005` | `timed-out` | `official validation timed out after 0.500 s and the process group was killed` |
| `cancel` set mid-run | `OFF001` | `cancelled` | `official validation was cancelled by the caller` |
| runner replaced by `/usr/bin/python3` | `OFF006` | `import-failed` | `failed to import schrodinger.application.desmond.enhsamp: No module named 'schrodinger.application'` |
| runner replaced by `/bin/true` | `OFF006` | `no-result` | `the validation worker produced no JSON result (exit code 0). stderr: (empty)` |
| accepted under 2020-3 | `OFF007` | `untested-release` | `Desmond release 2020-3 build 139 has not been tested against this interface; its verdict still stands, but the way its messages are read here may be out of date` |

Every row above was produced by running the adapter, not by reading the source.

---

## 3. Finding an installation

`discover_installations()` is pure filesystem inspection — nothing is executed,
because the GUI calls it on the main thread while the window is being built
(`source_window.py:333`).

Search order (`adapter._SEARCH_GLOBS`):

1. `$SCHRODINGER`, if set — first, and reported whether or not it is usable,
   because a wrong value there is the most common reason validation goes quiet.
2. `/opt/schrodinger*`
3. `~/schrodinger*`
4. `/usr/local/schrodinger*`

Candidates from 2–4 are sorted by path and de-duplicated by `os.path.realpath`,
so a symlink and its target are one entry. A directory is a candidate if it
carries any of `run`, `version.txt`, `licenses`, or a `mmshare-v*` directory
(`_looks_like_suite`); the licence landmark is there on purpose so that a
licence-only stub is *reported as unusable* rather than silently skipped.

Real output on this machine:

```
Installation(path='/opt/schrodinger', version='', build='', runner='/opt/schrodinger/run', usable=False, note="no 'run' wrapper in this directory")
Installation(path='/opt/schrodinger2020-3', version='2020-3', build='139', runner='/opt/schrodinger2020-3/run', usable=True, note='')
Installation(path='/opt/schrodinger2025-3', version='2025-3', build='160', runner='/opt/schrodinger2025-3/run', usable=True, note='')
```

Same thing through the CLI (`--list-installations` still requires a file
argument, an argparse wart in `potcheck.py`; pass any `.pot`):

```
$ python -m funnelforge.potcheck --list-installations tests/fixtures/minimal.pot
/opt/schrodinger  ? build ?  no 'run' wrapper in this directory
/opt/schrodinger2020-3  2020-3 build 139  usable
/opt/schrodinger2025-3  2025-3 build 160  usable
```

`/opt/schrodinger` really is a stub — it contains one directory, `licenses`,
and no `run`.

### What "usable" means

`_inspect()` sets `usable=True` only when all of these hold:

| Check | Failure note |
| --- | --- |
| the path is a directory | `not a directory` |
| `<root>/run` exists and is a regular file | `no 'run' wrapper in this directory` |
| `<root>/run` is executable by this user | `'run' is not executable by this user` |
| `<root>/internal/lib/python3*/site-packages/schrodinger` matches at least one path | `no schrodinger Python package under internal/lib` |

Version and build come from parsing `<root>/version.txt` — on this machine
`Schrodinger Suite 2025-3, Build 160` and `Schrodinger Suite 2020-3, Build 139`
— with the regex `Suite\s+(...)\s*,\s*Build\s+(...)`. A missing or unparseable
`version.txt` leaves both empty and does not make an installation unusable.

`usable` is never established by executing anything. A suite that passes all
four checks and still cannot import `schrodinger.application.desmond` fails at
run time as `OFF006` / `import-failed`, which is exactly what happened when the
runner was swapped for `/usr/bin/python3`.

### Which one is used

`default_installation()`: an explicit and usable `$SCHRODINGER` always wins;
otherwise the newest usable suite, ordered by `_version_key` — the integers in
the version string as a tuple, then the build number. On this machine that
selects `/opt/schrodinger2025-3`.

Both front ends defer to the same function. `potcheck --schrodinger PATH` picks
by realpath from the discovered list and exits 3 if there is no match. The GUI
fills a combo box with the usable installations and pre-selects whichever
`default_installation()` would have chosen, "so the toolbar and the headless CLI
cannot disagree about the engine" (`source_window.py:339`).

---

## 4. What actually runs

### The argv

Four elements, no shell anywhere, handed to `execve` exactly as given. From a
real manifest:

```json
"argv": [
  "/opt/schrodinger2025-3/run",
  "/home/bugra/Claude/potantial/funnelforge/core/desmond/_worker.py",
  "/home/bugra/Downloads/funnel_metadaynamics_ayrilma_Z5_run.cms",
  "/tmp/funnelforge-official-jxveynlt/potential.pot"
]
```

`Installation.argv_for` builds it as `[runner, worker_path, cms_or_dash,
pot_path]`. The suite's `run` is a `/bin/sh` script that accepts a `.py` file
directly and executes it under the suite's own interpreter — Python 3.11.4 for
2025-3, Python 3.6.2 for 2020-3.

The third element is `-` when no topology was supplied. The adapter never gets
that far: `validate()` refuses before spawning anything (`cms_path is None` →
`topology-required`). The `-` branch in the worker is reachable only by invoking
`_worker.py` by hand.

`subprocess.Popen` is called with `shell=False` (the default), `cwd=workdir`,
`stdin=subprocess.DEVNULL`, both other streams piped, `start_new_session=True`
and `close_fds=True`. The user's text is never interpolated into a command
line; it goes to disk as a file and the file's path is an argv element.

### The worker

`_worker.py` imports two modules from the suite, calls them, and prints exactly
one JSON object on stdout. It is written for Python 3.6 on purpose — no
`from __future__ import annotations`, no f-strings, no PEP 604 unions, no
dataclasses — because the user may point the adapter at Suite 2020-3, and a
worker that would not even compile there would report "validator crashed" for
every file. Verified: a run under `/opt/schrodinger2020-3/run` succeeds.

The JSON contract has every key always present:

```json
{"worker_version": "1.0.0", "ok": true, "error": "", "etype": "", "traceback": "",
 "compiled_chars": 3261, "schrodinger_version": "2025-3", "engine_output": "",
 "stage": "done", "read_cms_s": 1.22865891456604, "parse_s": 0.01090097427368164,
 "python_version": "3.11.4"}
```

`stage` is the field that keeps a bad `.cms` from being reported as a bad
`.pot`. The adapter reads it in `validate()`:

- `stage == "read_cms"` → reason is rewritten to `topology-unreadable` and the
  message becomes `the topology could not be read: …`. Without this, the
  engine's `Failed opening MAE file at …` would be classified as a rejection
  and shown to the user as if their expression were at fault.
- `stage == "read_potential"` → `no-result`.

Exit codes: `0` the engine ran (`ok` says what it decided), `2` wrong argv, `3`
imports failed, `4` declined for want of a topology, `5` topology unreadable,
`6` the `.pot` could not be read. The adapter does not branch on the exit code;
it branches on the JSON. The exit code is recorded in the manifest.

Everything between the two imports and the end of parsing runs inside
`_FdCapture`, which `os.dup2`s file descriptors 1 and 2 onto a
`tempfile.TemporaryFile`. Descriptor level, not `io` level, for two measured
reasons.

First, `enhsamp.resolve_atomsel` (`enhsamp.py:715`) manipulates descriptors
itself — it redirects stdout onto stderr for the duration so that VMD's banner
does not land in the caller's stdout:

```python
sout = os.dup(sys.stdout.fileno())
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
```

An `io.StringIO` in `sys.stdout` has no descriptor, so this raises. Verified by
running `contextlib.redirect_stdout(io.StringIO())` around a `parseStr` call
that contains an `atomsel`:

```
io.StringIO redirect -> UnsupportedOperation: fileno
```

Second, ANTLR writes syntax errors to fd 2 below anything Python-visible.

Nothing may print inside that block — fd 1 is the temporary file — so the single
JSON object is emitted once, after it.

### The environment

Built from scratch in `_worker_environment`. Nothing is inherited except four
named variables, so a stray `PYTHONPATH`, `PYTHONHOME`, `LD_LIBRARY_PATH` or an
active conda environment cannot reach into the suite.

| Variable | Value | Why |
| --- | --- | --- |
| `SCHRODINGER` | the installation path | the `run` wrapper needs it |
| `PATH` | `/usr/bin:/bin:/usr/sbin:/sbin` | `run` is a `/bin/sh` script that shells out to `uname`, `sed`, `ls` |
| `HOME` | `os.path.expanduser("~")` | licensing |
| `TMPDIR`, `TMP`, `TEMP` | the run's own working directory | keeps the suite's scratch inside the directory that gets deleted |
| `LANG`, `LC_ALL` | `C.UTF-8` | deterministic message text |
| `PYTHONIOENCODING` | `utf-8` | the JSON on stdout |
| `PYTHONDONTWRITEBYTECODE` | `1` | no `__pycache__` in the suite |
| `PYTHONUNBUFFERED` | `1` | output is not lost when the group is killed |
| `QT_QPA_PLATFORM` | `offscreen` | headless |
| `MPLBACKEND` | `Agg` | headless |

Forwarded from the parent when set, and only these: `HOME`, `LANG`,
`SCHROD_LICENSE_FILE`, `LM_LICENSE_FILE`.

### The temp directory

`tempfile.mkdtemp(prefix="funnelforge-official-")` — mode `0o700`, verified.
The potential is written into it as `potential.pot` with
`newline=""`, so the user's line endings survive verbatim, and the directory is
the child's `cwd` and `TMPDIR`. It is removed in a `finally` block with
`shutil.rmtree(..., ignore_errors=True)`, which runs whether the child
succeeded, timed out or was cancelled. Verified: no `/tmp/funnelforge-official-*`
directory survives a run, including the timed-out and cancelled ones.

Consequence for the manifest: `argv[3]` names a file that no longer exists by
the time you read it. Reproduction uses `pot_sha256`, not that path — see
[§7](#7-the-manifest).

The topology path is canonicalised before use by `_canonical_cms`:
`realpath` (resolving `..` and every symlink), then `os.path.exists`,
`stat.S_ISREG`, `os.access(..., R_OK)`. A symlink to a fifo, a device or a
directory is refused rather than handed to a subprocess. Real refusals:

```
topology not found: /home/bugra/Downloads/does-not-exist.cms
topology is not a regular file: /tmp
```

### Timeout and cancellation

Defaults: `adapter.validate(timeout=180.0)`, `potcheck --timeout` also 180.0,
the GUI passes `300.0` (`source_window.py:596`).

The parent polls every 10 ms for three conditions: the child exited, the
caller's `threading.Event` is set, or the deadline passed. Both pipes are
drained to EOF by daemon threads, so neither can fill and deadlock. On timeout
or cancellation `_kill_group` sends `SIGTERM` to the process group, waits 2 s,
then `SIGKILL`. `start_new_session=True` means the pgid is the child's pid, so
anything the suite forked is collected too.

Measured:

| Case | Wall time | `ran` | `exit_code` | Message |
| --- | --- | --- | --- | --- |
| `timeout=0.5` | 0.55 s | `False` | `-15` | `official validation timed out after 0.500 s and the process group was killed` |
| `cancel` set at 0.6 s | 0.62 s | `False` | — | `official validation was cancelled by the caller` |
| `cancel` already set | 0.00 s | `False` | `None` | `cancelled before the engine was started` |

`-15` is `-SIGTERM`: the child died to the first signal and never needed the
`SIGKILL`. A timeout is `OFF005` and a cancellation is `OFF001`; both leave
`ran=False`, because nothing was learned about the potential either way.

The GUI is stricter still. `OfficialWorker` is a `QThread` carrying the source
revision it was started against, and `_official_ready` discards the result when
the text moved underneath it:

```
the text changed while Desmond was parsing; that result describes older text and was discarded
```

### Output caps

Six caps, all of them "cut and say so plainly" rather than "drop":

| Cap | Value | Where |
| --- | --- | --- |
| engine descriptor capture | 64 KiB | `_worker._ENGINE_OUTPUT_CAP`; appends `...[engine output truncated]...` |
| traceback | 16 KiB | `_worker._TRACEBACK_CAP`; appends `...[traceback truncated]...` |
| each captured stream (`stdout`, `stderr`) | 256 KiB | `adapter.CAPTURE_CAP_BYTES`; appends `...[truncated: N of M bytes shown]...` |
| `error_message` | 400 chars | `adapter.MESSAGE_CAP_CHARS`, cut on a word boundary, `... (N characters in total)` appended |
| diagnostic evidence | 4 KiB | `_evidence()` |
| stderr carried as evidence on timeout / no-result | 2 KiB | `_reason_diagnostic(..., evidence=_truncate(stderr, 2048))` |

The message cap exists because a rejected ASL is quoted back twice with every
atom index, and `error_message` goes into a one-line GUI label. The full text is
still in `result.stdout` and in the evidence.

Reading the reply is also defensive: `_extract_json` tries the whole of stdout
first and then each line from the end, accepting only a JSON object that
contains an `ok` key, because the suite's `run` wrapper is entitled to print
lines of its own.

---

## 5. Inside the worker: `read_cms` then `parseStr`

Two calls, in this order (`_worker._run_engine`):

```python
_msys_model, cms_model = topo.read_cms(cms_path)
compiled = enhsamp.parseStr(cms_model, pot_text)
```

`parseStr` is the real entry point, not a validation-only side door. In
`/opt/schrodinger2025-3/internal/lib/python3.11/site-packages/schrodinger/application/desmond/enhsamp.py:745`
it parses the text, resolves atom selections against `system`, type-checks,
constant-folds, and returns the backend configuration string. The suite's own
job setup calls the same function: `config_utils.py:795`
(`s_expr = parseStr(model, m_expr)`) and `meta.py:615`
(`cfg_str = parseStr(model, mexpr)`). Passing this call is passing what the job
would do at start-up.

The M-expression is never evaluated, compiled or `exec`'d anywhere in
FunnelForge. It is a string handed to the engine.

### Why it needs a topology

`enhsamp.resolve_atomsel` builds an `ASLObject(system)`. With a structure it
calls `cms.select_atom(asl)` and then `cms.gid(indices)`. With `system=None` it
takes a branch the vendor marked `# FIXME`
(`enhsamp.py:664–692`), which matches only the literal pattern
`atom.\s+(.*)`, subtracts one from each index, and raises for anything else. The
vendor's own comment says the shortcut "will fail on restarting FEP with
metadynamics or the reaction coordinate happen to involve molecules with virtual
site."

### What actually happens without one — measured

Run directly under `/opt/schrodinger2025-3/run python3`, calling
`enhsamp.parseStr(None, text)`:

| Selection in the `.pot` | Result |
| --- | --- |
| `atomsel("atom. 1,2,3")` | **accepted**, `gids=[0 1 2]` |
| `atomsel("protein")` | `RuntimeError: Failed to get gid from asl ('protein') without structure.` |
| `atomsel("res. 10-20")` | `RuntimeError: Failed to get gid from asl ('res. 10-20') without structure.` |
| no selection at all | **accepted**, `gids=[]` |

The first row is the dangerous one. With the real 62,870-atom topology loaded,
the same file compiles to a byte-identical result:

```
with topology:    {type=enhanced_sampling gids=[0 1 2] sexp=[let [[$g [literal 0.0 1.0 2.0]]] [norm [center_of_mass $g]]] …}
without topology: {type=enhanced_sampling gids=[0 1 2] sexp=[let [[$g [literal 0.0 1.0 2.0]]] [norm [center_of_mass $g]]] …}
```

They agree here because `gid == atid - 1` happens to hold for this system. That
is precisely why a pass without a topology proves nothing: the shortcut can
agree by accident, and when it disagrees it does so silently, producing a
potential that biases the wrong atoms.

So `validate()` refuses before spawning anything when `cms_path is None`, with
the full `NO_TOPOLOGY_REASON` paragraph as the message, `ran=False`, and
`OFF001`. Every clause of that paragraph was checked against the engine above.

An unreadable or non-`.cms` file that passes the `_canonical_cms` checks gets as
far as the worker and fails in `topo.read_cms`; `stage == "read_cms"` then
converts the verdict to `topology-unreadable`, `ran=False`, `OFF001`.

---

## 6. `parseStr` raises `SystemExit`, not an exception

This is the single most important implementation fact about the engine, and it
is why every handler in `_worker.py` catches `BaseException`.

`mexpParser.reportError`
(`.../enhanced_sampling/mexpParser.py:219`) is:

```python
def reportError(self, err):
    import sys
    BaseRecognizer.reportError(self, err)
    sys.stderr.write("unable to parse m-expression\n")
    exit(1)
```

`mexpLexer.py:101` does the same. `exit(1)` raises `SystemExit`, which does not
inherit from `Exception`.

Verified by running it, under `/opt/schrodinger2025-3/run python3`, on
`declare_output(...);\nnorm(pos(1)\n`:

```
line 0:-1 mismatched input '<EOF>' expecting PCLOSE
unable to parse m-expression
CAUGHT type=SystemExit mro=['SystemExit', 'BaseException', 'object'] repr=SystemExit(1)
isinstance Exception = False
isinstance BaseException = True
```

and the same call wrapped in `except Exception`:

```
escaped `except Exception`: SystemExit SystemExit(1)
```

Three consequences the code has to handle, and does:

1. **`except Exception` is not enough.** A plain typo in a `.pot` would kill the
   worker with no JSON on stdout, and the adapter would report `OFF006`
   "validator crashed" for every syntax error in the language. `_run_engine`,
   the import block, the potential read and `_FdCapture` all catch
   `BaseException`.
2. **`str(SystemExit(1))` is `"1"`.** The real complaint is on the descriptor,
   not in the exception. `_resolve_message` runs *after* the capture has been
   read and substitutes the captured lines for the useless `"1"`.
3. **The position is only in the ANTLR text.** `_antlr_positions` parses
   `^line\s+(-?\d+):(-?\d+)\s+(.*)$` out of the captured output and shifts the
   column, which ANTLR reports 0-based, to the 1-based convention used
   everywhere else in this application. `_classify` checks for a syntax error
   first — before any exception-type branch — because `SystemExit` would
   otherwise fall through to the generic `engine-error` case with the message
   `"1"`.

Real end-to-end result on `comment_styles.pot`: ANTLR printed
`line 42:79 no viable alternative at character '<EOF>'`, and the adapter
reported `OFF003` at line 42, column 80.

---

## 7. The manifest

Every run produces one, whether or not it reached a verdict. It is on
`OfficialResult.manifest`, and `potcheck --json` emits it under `"official"`.

### Fields

Always present, from `adapter._manifest`:

| Field | What it is | For |
| --- | --- | --- |
| `pot_sha256` | SHA-256 of the source text as UTF-8 | identifies the exact text judged; the reproduction key |
| `pot_bytes` | length of that UTF-8 encoding | sanity check against a truncated copy |
| `cms_sha256` | SHA-256 of the topology bytes, read in 1 MiB chunks | identifies the exact structure; `null` when no run happened |
| `cms_path` | the canonicalised topology path (symlinks resolved) | says which file that hash came from |
| `schrodinger_path` | the installation root | which suite |
| `version`, `build` | e.g. `"2025-3"`, `"160"`, parsed from `version.txt` | which release |
| `runner` | absolute path to the suite's `run` | argv element 0 |
| `argv` | the full argument list, verbatim | the command; empty list when nothing was spawned |
| `exit_code` | the child's status, `null` when nothing was spawned | `-15` means the group was signalled |
| `timed_out` | the deadline was hit and the group killed | distinguishes "unknown" from "rejected" |
| `cancelled` | the caller's event stopped it | same |
| `timestamp_utc` | ISO-8601 UTC at the start of the call | ordering, audit |
| `duration_s` | wall clock for the whole call, 4 dp | includes suite start-up |
| `adapter_version` | `"1.0.0"` | bumped when the manifest or verdict semantics change |

Added when the worker replied with parseable JSON:

| Field | What it is |
| --- | --- |
| `worker_version` | `"1.0.0"`; bumped when the JSON contract changes |
| `schrodinger_release` | `schrodinger.get_release_name()` from inside the suite — the release as the suite itself reports it, independent of `version.txt` |
| `read_cms_s` | seconds inside `topo.read_cms` |
| `parse_s` | seconds inside `enhsamp.parseStr` |

Added on acceptance only:

| Field | What it is |
| --- | --- |
| `compiled_chars` | length of the backend configuration string the engine produced |

### A real example

`tests/fixtures/funnel_general.pot` against the Z5 topology:

```json
{
  "pot_sha256": "8224ab25dd0b25ae166f540a36775c0f1cf0474b9f2f2ac50cb2fa528bfe2f36",
  "pot_bytes": 3454,
  "cms_sha256": "5ac80af085e7f5d4951ffdc3b3be4c7b3ebc72891eaaac6c82a158c9e93043bf",
  "cms_path": "/home/bugra/Downloads/funnel_metadaynamics_ayrilma_Z5_run.cms",
  "schrodinger_path": "/opt/schrodinger2025-3",
  "version": "2025-3",
  "build": "160",
  "runner": "/opt/schrodinger2025-3/run",
  "argv": [
    "/opt/schrodinger2025-3/run",
    "/home/bugra/Claude/potantial/funnelforge/core/desmond/_worker.py",
    "/home/bugra/Downloads/funnel_metadaynamics_ayrilma_Z5_run.cms",
    "/tmp/funnelforge-official-jxveynlt/potential.pot"
  ],
  "exit_code": 0,
  "timed_out": false,
  "cancelled": false,
  "timestamp_utc": "2026-08-28T20:34:15.595050+00:00",
  "duration_s": 1.8609,
  "adapter_version": "1.0.0",
  "worker_version": "1.0.0",
  "schrodinger_release": "2025-3",
  "read_cms_s": 1.2347657680511475,
  "parse_s": 0.010608196258544922,
  "compiled_chars": 3261
}
```

A refused run carries the same shape with the evidence of refusal in it — here,
no topology:

```json
{"pot_sha256": "c9e484d386ae012a569c96ed8dbcc373c9a1aad14260732121cca120cb75fa65",
 "pot_bytes": 113, "cms_sha256": null, "cms_path": null,
 "schrodinger_path": "/opt/schrodinger2025-3", "version": "2025-3", "build": "160",
 "runner": "/opt/schrodinger2025-3/run", "argv": [], "exit_code": null,
 "timed_out": false, "cancelled": false,
 "timestamp_utc": "2026-08-28T20:34:49.084680+00:00", "duration_s": 0.0009,
 "adapter_version": "1.0.0"}
```

`argv: []` and `exit_code: null` say plainly that nothing was executed.

### Reproducing a run from it

1. Get the source text whose SHA-256 is `pot_sha256` and write it to any path.
   The `argv[3]` path was inside a temporary directory that has already been
   deleted; the hash is the identity, the path is not.
2. Check the topology: `sha256sum <cms_path>` must equal `cms_sha256`. If it
   does not, you are reproducing a different experiment.
3. Run `argv` with element 3 replaced by your path, under the environment from
   [§4](#the-environment).

Done by hand, with the same fixture:

```
$ sha256sum /tmp/.../potential.pot
8224ab25dd0b25ae166f540a36775c0f1cf0474b9f2f2ac50cb2fa528bfe2f36  /tmp/.../potential.pot

$ env -i SCHRODINGER=/opt/schrodinger2025-3 PATH=/usr/bin:/bin:/usr/sbin:/sbin \
      HOME="$HOME" LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8 \
      PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QT_QPA_PLATFORM=offscreen \
      MPLBACKEND=Agg TMPDIR=/tmp/... \
    /opt/schrodinger2025-3/run \
      /home/bugra/Claude/potantial/funnelforge/core/desmond/_worker.py \
      /home/bugra/Downloads/funnel_metadaynamics_ayrilma_Z5_run.cms \
      /tmp/.../potential.pot

{"worker_version": "1.0.0", "ok": true, "error": "", "etype": "", "traceback": "",
 "compiled_chars": 3261, "schrodinger_version": "2025-3", "engine_output": "",
 "stage": "done", "read_cms_s": 1.22865891456604, "parse_s": 0.01090097427368164,
 "python_version": "3.11.4"}
exit=0
```

Same `pot_sha256`, same `compiled_chars` (3261), same verdict. The manifest is
sufficient to reproduce or dispute a verdict without the application.

What the manifest does *not* pin: the contents of the installation at
`schrodinger_path`. If the suite is upgraded in place, the same manifest can
produce a different answer, and only `version`/`build`/`schrodinger_release`
would show it.

---

## 8. What one validation costs

Wall clock is dominated by reading the topology, not by parsing the potential.
All figures from this machine, 2026-08-28, warm page cache, on the 48.8 MB /
62,870-atom Z5 `.cms`.

Through the adapter (`duration_s`, `read_cms_s` and `parse_s` from real
manifests; "rest" is the arithmetic remainder — the suite's `run` wrapper,
interpreter start-up, importing `enhsamp` and `topo`, and the JSON round trip):

| Potential | Bytes | Total | `read_cms` | `parseStr` | Rest |
| --- | ---: | ---: | ---: | ---: | ---: |
| `minimal.pot` | 113 | 1.867 s | 1.247 s | 0.0031 s | 0.617 s |
| `minimal.pot` | 113 | 1.849 s | 1.219 s | 0.0033 s | 0.627 s |
| `minimal.pot` | 113 | 1.847 s | 1.220 s | 0.0028 s | 0.624 s |
| `funnel_general.pot` | 3454 | 1.856 s | 1.222 s | 0.0105 s | 0.624 s |
| `funnel_general.pot` | 3454 | 1.846 s | 1.221 s | 0.0109 s | 0.614 s |
| `funnel_general.pot` | 3454 | 1.846 s | 1.223 s | 0.0105 s | 0.613 s |
| `metad_2d_wt.pot` | 1888 | 2.002 s | 1.388 s | 0.0076 s | 0.606 s |
| `deep_nesting.pot` (rejected) | 1139 | 2.172 s | 1.392 s | 0.0035 s | 0.777 s |

In-process under `/opt/schrodinger2025-3/run python3`, with no subprocess and
no JSON, three consecutive `topo.read_cms` calls and five consecutive
`parseStr` calls on `funnel_general.pot`:

```
read_cms_s              [1.206, 1.181, 1.167]
parse_s funnel_general  [0.0087, 0.0079, 0.0085, 0.0077, 0.0082]
```

The split, for a typical accepted file:

| Stage | Share of ~1.85 s |
| --- | --- |
| `topo.read_cms` | ~1.22 s (66%) |
| suite start-up and imports | ~0.62 s (33%) |
| `enhsamp.parseStr` | ~0.010 s (0.6%) |

Under Suite 2020-3 the same file took 2.96 s total: `read_cms` 2.427 s,
`parseStr` 0.021 s, rest 0.512 s — about twice the topology cost of 2025-3.

Two things follow. First, validation is far too slow to run on every keystroke,
which is why the GUI puts it behind an explicit "Validate with Desmond" action
on a cancellable thread while the local layers run on a debounce. Second,
optimising the parse would buy nothing: it is 0.6% of the cost. The whole
expense is reading the structure, and a fresh subprocess is started for every
call, so nothing is reused between validations. Repeating `topo.read_cms`
inside one process does not get cheaper either (1.206, 1.181, 1.167 s), so the
suite is not caching it internally.

---

## 9. When the interface and the engine disagree

**The engine wins. Always.** `OFF003` is documented as taking "precedence over
anything else this interface reports, including any check that passed", and it
is the only official code that blocks a run-ready export. There is no path by
which a local layer's opinion overrides a verdict, and no path by which a local
layer's success produces one.

The two directions are not symmetric.

Measured over the whole 30-file fixture corpus (`tests/fixtures/`), each file
put through `run_local_layers()`, `resolve_topology(Z5)` and
`validate_official(Z5)`:

| | Engine accepts | Engine rejects |
| --- | ---: | ---: |
| **Interface has no error** | 20 | 3 |
| **Interface has an error** | **0** | 7 |

**Interface errors, engine rejects** — agreement, 7 fixtures:

| Fixture | Interface's error codes |
| --- | --- |
| `arbitrary_names.pot` | `SYN001` |
| `bad_accumulator.pot` | `MTD003` |
| `dependency_cycle.pot` | `SEM003` |
| `dimension_mismatch.pot` | `MTD004`, `MTD005` |
| `duplicate_assign.pot` | `SEM002` |
| `empty_selection.pot` | `TOP001`, `TOP002` |
| `undefined_symbol.pot` | `SEM001` |

**Interface silent, engine rejects** — the interface being incomplete, and the
whole reason the adapter exists. 3 fixtures:

| Fixture | Engine's message | What the interface said |
| --- | --- | --- |
| `comment_styles.pot` | `no viable alternative at character '<EOF>'` | `SYN006`, a warning |
| `deep_nesting.pot` | `maximum recursion depth exceeded while calling a Python object` | nothing |
| `future_function.pot` | `unknown function 'quantum_cv'` | two `MOD003` warnings |

Each of these is deliberate. `future_function.pot` calls a name the registry
does not know, and the interface's rule is that unknown means unrecognised, not
invalid — `MOD003` says "preserved unchanged and left for the official validator
to judge". `comment_styles.pot` gets a warning because the fix is one keystroke.
`deep_nesting.pot` is well formed under the grammar; it is the vendor's
recursive-descent parser that overruns its stack, which is a property of that
implementation, not of the language.

**Interface errors, engine accepts.** A false positive — the interface calling
something broken that Desmond is happy with. **Zero occurrences across all 30
fixtures.** This is the direction that would actually harm a user, by telling
them to rewrite working input, and the corpus does not contain a case of it.
That is evidence, not a proof: the corpus is 30 files.

The topology layer matters to this tally. Run without `resolve_topology`,
`empty_selection.pot` moves into the "interface silent" row, because `TOP001`
and `TOP002` can only be raised against a real structure. A validation run that
skips the topology layer is a weaker comparison than the one above.

Against the recorded corpus in `tests/fixtures/differential.json`: all 30
`official_ok` values match a fresh run exactly, and all 30 `engine` messages
match. One `interface_error` value does not — `arbitrary_names.pot` is recorded
as `false`, but the current code reports `SYN001`, *declarations belong at the
top of the file*, which agrees with the engine
(`no viable alternative at input 'static'`) and with that fixture's own
`"expect_parses": false` in `manifest.json`. That row is stale.

Note also that `differential.json`'s `interface_error` column was computed
without the topology layer: it records `empty_selection.pot` as clean, which is
only true when no structure has been resolved. Read it as the no-topology
comparison, not the one tabulated above.

### What to do with a disagreement

- Engine rejects, interface silent → the interface has a gap. The user is
  correctly blocked from exporting; file the gap against the local layer.
- Engine accepts, interface errors → the interface is wrong. The user is being
  blocked from a valid file, which is the worse failure. `export_run_ready`
  takes `force=True` for exactly this, and it still returns the blockers it
  overrode.
- Engine and interface both reject with different reasons → believe the
  engine's message. The interface's is a second opinion, not a translation.

---

## 10. Known gaps

Stated plainly, because a document that only lists what works is not useful in
review.

1. **A topology change does not make the verdict read "out of date".**
   `Document.validate_official` records `source_rev=rev` — the source hash
   only — and `Document._invalidate` calls `report.stale(self.rev)` with no
   per-layer keys. `diagnostics.combined_rev()` exists precisely to key the
   official layer on *(text, topology)*, and **nothing outside
   `diagnostics.py`'s own self-test calls it**. Measured: after a successful
   validation, changing `topology_digest` gives

   ```
   official_is_current: False
   export_blockers:     [('OFF009', 'the topology changed after the last official validation; …')]
   report state:        State.PASSED
   verdict:             Desmond-valid: the official parser accepted this exact text. …
   ```

   The export gate is correct; the headline sentence is not. Against the
   contract in §1, point 4 holds for the source hash and holds for the topology
   hash *only at the export gate*.

2. **`State.WARNING` on the official layer is unreachable through `Document`.**
   The `Desmond accepted this file, with warnings from the engine` verdict never
   appears; `OFF010` is filed as a diagnostic but the layer is marked `PASSED`.

3. **`OFF006` conflates two situations.** `import-failed` (a broken
   installation) and `no-result` (the worker produced nothing) both surface as
   "Official validator crashed". The distinction survives only in
   `data["reason"]`.

4. **`potcheck --list-installations` still requires a file argument.** argparse
   declares `files` as required with `nargs="+"`, so the flag cannot be used on
   its own; it exits 2 with a usage message.

5. **Not exercised here:** the `-` (no-topology) argv branch inside the worker,
   which `validate()` refuses to reach; the 256 KiB, 64 KiB, 16 KiB and 400-char
   truncation paths; `EXIT_USAGE` (2) and `EXIT_NO_POTENTIAL` (6). These were
   read, not run. Worker exit codes `0`, `3` and `5` were run, as was every
   adapter refusal branch. Exit 5, for completeness: handing the adapter a plain
   text file as the topology gives `ran=False`, `exit_code=5`, `OFF001` /
   `topology-unreadable`, message `the topology could not be read: Line 1
   predicted '{', have 'is'` — the engine's own complaint, correctly attributed
   to the `.cms` rather than to the potential.

6. **Nothing verifies the installation itself.** The adapter controls how the
   suite is invoked and with what environment; it does not check that the
   binaries under `$SCHRODINGER` are the vendor's. Anyone who can write there
   can already run code as the user.
