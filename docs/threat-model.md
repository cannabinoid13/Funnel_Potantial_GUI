# Threat model

FunnelForge opens files that a scientist did not write, on a workstation that
usually holds cluster credentials, an SSH agent, a Schrodinger licence token
and unpublished structures. A `.pot` arrives by e-mail from a collaborator; a
`.cms` comes out of a workflow on a shared machine; a finished run comes back
as a ZIP from a cluster that several groups can write to. All of that is
input, and none of it is trusted.

This document says what an attacker could try, what stops it, where that code
lives, and what is deliberately not defended. It is meant to be argued with
during review: if a change makes one of the claims below untrue, the change is
wrong or this file is.

---

## 1. Assets

| Asset | Why it is worth attacking |
| --- | --- |
| The scientist's account on the workstation | SSH keys and agent sockets, cluster access, mounted group shares, the licence token |
| Unpublished structures and potentials | Pre-publication IP; the whole point of the tool is that they are valuable |
| The exported job set (`.pot`, `.cfg`, `.msj`, `.sh`, `.cms`) | It is executed on a cluster, by a queue, with the user's identity |
| Scientific correctness of the export | A silently wrong potential wastes GPU-months and can put a wrong number in a paper. This is the highest-probability harm in the list, and it is why "lossless" is a security property here, not a nicety |

## 2. Trust boundaries

| Input | Trust | Reasoning |
| --- | --- | --- |
| `.pot` | **Untrusted** | Text in a small imperative language, hand-edited and mailed around. Parsed by our code, evaluated by our interpreter |
| `.cms` / `.mae` | **Untrusted** | Machine-generated but attacker-influenceable: sizes, atom counts, indices and property strings all come from the file |
| `.cfg` / `.msj` / `.sh` in an imported bundle | **Untrusted** | Brace-structured text; the `.sh` additionally carries a job name that reaches a shell |
| ZIP run packages | **Untrusted** | Member names, member types, declared sizes and compression ratios are all attacker-chosen |
| Session `.json` | **Untrusted** | It travels with a project and is exactly as trustworthy as whoever sent it |
| `$SCHRODINGER` and the installation under it | **Semi-trusted configuration** | The user chooses the path; we execute what is there. We do not verify Schrodinger's binaries, but we do control *how* they are invoked and with what environment |
| The user at the keyboard | **Trusted** | They can already run anything they like. We defend them from files, not from themselves |
| `funnelforge/**` on disk | **Trusted** | An attacker who can write our code has already won; that is the operating system's boundary, not ours |

## 3. Adversary

The realistic adversary is not targeting this program. They send a plausible
funnel-metadynamics project to a lab, or leave a run package on a shared
filesystem, and rely on the file being opened. They can choose every byte of
every input file, they can create and swap symlinks in directories the user
can write to (`/tmp`, a shared scratch, the project directory), and they can
be patient. They cannot execute code as the user beforehand, and they cannot
modify the installed application.

A second, more likely adversary is an accident: a corrupt file, a truncated
download, a 40 GB trajectory dropped in the wrong slot. The defences are
mostly the same, which is a good sign.

### Where the defences live

Four layers handle untrusted bytes, and every threat below names one of them:

| Layer | Modules | Responsibility |
| --- | --- | --- |
| Safety primitives | `core/safety.py` | Bounded reads, exact encodings, canonical paths, atomic writes, archive extraction, scrubbed child environments |
| Lossless front end | `core/lang/` (`lexer`, `cst`, `patch`) | Text to tokens to spans, and edits applied as patches to spans so nothing is regenerated |
| Semantic model | `core/lang/` (`sema`, `registry`, `diagnostics`), `core/mexpr.py` | What the file means, what the installed Desmond accepts, and what to tell the user; a model, never authority |
| Official validator | `core/desmond/` (`adapter`, `_worker`) | The only code that may call a potential Desmond-valid, and the only code that starts a child process |

---

## 4. Threats

### T1. Code execution through expression evaluation

The `.pot` file is a program. The obvious way to lose is to evaluate it with
Python's `eval`, `exec`, `compile`, or to hand a name from the file to
`getattr`/`__import__`.

The M-expression language is instead lexed (`core/lang/lexer.py`), parsed into
a tree of spans (`core/lang/cst.py`), analysed by walking that tree
(`core/lang/sema.py`) and, where a preview value is wanted, interpreted
explicitly (`core/mexpr.py`). The set of callable names is a closed table
(`core/lang/registry.py`, `mexpr.FUNCTIONS`); an unknown name is a diagnostic,
never a lookup into Python's namespace. The official validator hands the text
to Schrodinger's engine as an opaque string (T14) rather than evaluating it.

There is no `eval`, `exec`, `compile`, `os.system` or `shell=True` anywhere in
`funnelforge/`. That is an invariant a reviewer can check in one line; the
only hits today are a `pickle.load` (see T9b) and a docstring:

```
grep -rnE "\beval\(|\bexec\(|os\.system|shell=True|__import__|pickle\.load" \
     --include=*.py funnelforge/
```

### T2. Command injection through file and job names

`core/jobfiles.py` renders the launcher from `SH_TEMPLATE`, which contains
`JOBNAME="{jobname}"`, and `core/project.py` builds `f"{jobname}.pot"` and
friends. The job name is *not* typed fresh by the user in the import path: it
comes from the stem of an imported file or from the `JOBNAME` line of an
imported `.sh`. A name containing a double quote and `$(...)` therefore ends
up inside a shell script the user is expected to run.

This is a live gap, listed in section 7 with its fix. What is already in place
is the other half: any child process this application starts must be started
without a shell and with `safety.minimal_env()`, so a name that does reach a
command line cannot also bring `LD_PRELOAD` or `PYTHONPATH` with it.

### T3. Path traversal on export

Export takes a directory from the user and writes five files whose names come
from the job name. A job name of `../../.ssh/authorized_keys` would place a
file outside the chosen directory. `safety.canonical_path` normalises and
absolutises every path the IO layer touches, and `safety.atomic_write` refuses
to create the destination directory implicitly, so a traversal cannot quietly
manufacture a tree. Constraining the *name* is the job-name whitelist in
section 7.

### T4. Path traversal and symlink abuse during extraction

A ZIP member may be called `/etc/cron.d/x`, `../../.bashrc`, `..\\x` (a
traversal only on Windows), `C:evil` (drive-relative, which `ntpath.isabs`
calls relative), or `CON`. A member may be a symlink whose target is
`/etc/passwd`, so that a *later* member writes through it. A member may be a
character device. A directory the archive creates may itself be a link out of
the destination, and a link may already be sitting in the destination when
extraction starts.

`safety.safe_extract_zip` never calls `ZipFile.extract`, which trusts the
name. It re-validates every name under the union of POSIX and Windows rules,
joins it onto the destination itself, checks that the *resolved* parent stays
inside the destination, refuses any member that is not a regular file or a
directory, and opens each output with `O_CREAT|O_EXCL|O_NOFOLLOW` so a planted
link is neither followed nor overwritten. Extracted files are created `0o600`
regardless of the mode stored in the archive.

### T5. Decompression bombs

A 16 KB archive that declares 16 MB of payload, or 42 KB that declares
4.5 PB. `safe_extract_zip` caps the archive file itself, the uncompressed size
of any single member, the running total for the archive, the number of
members, and the uncompressed-to-compressed ratio (default 200x, applied only
above 1 KB where the ratio is meaningful). All of those are checked against
the central directory *before* a byte is written, and the running total is
re-checked as bytes land, so a header that lies in either direction is caught:
understating is caught by CPython's own truncation plus the CRC, overstating
by the pre-checks.

### T6. Resource exhaustion through deeply nested or huge expressions

Every parser here recurses over the tree, and a lexer will happily tokenise a
200 MB file. A `.pot` consisting of 100 000 nested parentheses is a
`RecursionError` — in a PyQt application that is a crash, and a crash during
import loses the scientist's unsaved work. A `series (i=0:10^9)` is worse: it
is a hang with no cancel button.

Every stage now has a ceiling. `safety.read_text_file` caps the bytes read
before a parser ever sees them; `lang/lexer.tokenize` refuses past
`max_tokens` (2 000 000); `lang/cst._Parser._expr` counts nesting and raises
at `max_depth` (256) rather than letting CPython hit its own recursion limit;
`lang/sema.analyse` bounds the dependency walk with `max_dependency_depth`
(512) and reports `SEM004`; `mexpr` refuses a series of more than 200 000
iterations. `safety.Limits` holds the same numbers as the one place to tune
them, and `enforce_limit` gives every ceiling the same refusal wording — see
open item 7.3, because those numbers are currently written twice.

### T7. Symlink swap between validation and save (TOCTOU)

The Check tab validates a path; some seconds later the user presses Save. In
between, anything on a writable path can be replaced by a symlink to
`~/.ssh/authorized_keys` or to a colleague's file.

Two things make this a non-event. `atomic_write` writes a temp file in the
destination's directory and `os.replace`s it: `os.replace` swaps a *directory
entry*, so a symlink sitting at the destination is destroyed and replaced by
our regular file — the link's target is never opened, let alone written.
Second, `expect_digest` lets the caller state what it believes is on disk;
if the destination changed at all since it was read, `ExternalChange` is
raised before the temp file is even created. `expect_digest=""` asserts "this
file must not exist yet", which is how a new export refuses to clobber.

For the cases where following a link would itself be the privilege change,
`canonical_path(..., allow_symlink=False)` refuses a link anywhere in the
chain, not only at the last component.

### T8. Information leak through temp files and telemetry

Temp files are the leak nobody notices: `tempfile.mkstemp` in a shared `/tmp`,
with the potential in it, readable by anyone. `atomic_write` puts its temp
file in the *destination's* directory (needed for atomicity anyway), gives it
mode `0o600` via `mkstemp`, and removes it on any failure. A brand-new
destination is created `0o600`; an existing one keeps the mode it had, because
`os.replace` would otherwise donate the temp file's.

The validator's worker gets its own temporary directory, which becomes its
`TMPDIR`/`TMP`/`TEMP` so the suite cannot scatter intermediates elsewhere, and
the directory is removed when the run ends.

FunnelForge sends nothing anywhere. There is no telemetry, no crash reporter,
no update check and no network code in `funnelforge/`:

```
grep -rnE "(^|[^.\w])(import|from) +(socket|urllib|http|ftplib|smtplib|requests)\b|urlopen\(" \
     --include=*.py funnelforge/
```

returns nothing. The environment is read in exactly four places —
`QT_AUTO_SCREEN_SCALE_FACTOR` in `__main__.py`, `XDG_CACHE_HOME` in `cms.py`,
`SCHRODINGER` and the forwarded licence variables in `desmond/adapter.py`, and
the allowlist in `safety.minimal_env`. Keep it that way: a molecular design
tool with an outbound socket is a different product.

### T9. Untrusted `.cms`: indices, sizes, and the pickle cache

Atom indices from a file are used to subscript coordinate arrays;
`mexpr.atomsel` already range-checks against `st.n_atoms` and refuses an
out-of-range index with a message. Atom counts bound memory and VTK
allocation, which `max_file_bytes` bounds in turn.

The real problem is `core/cms.py`, which caches a parsed structure with
`pickle.dump` and reloads it with `pickle.load` from
`$XDG_CACHE_HOME/funnelforge/<sha1>.pkl`. Unpickling is arbitrary code
execution by design. The cache lives in the user's own directory, so this is
not remotely reachable, but it converts "attacker can write one file in your
cache directory, or set one environment variable" into "attacker runs code as
you". It is listed as an open item in section 7 with its fix.

### T10. Environment injection into child processes

The official validator runs `$SCHRODINGER/run .../_worker.py`. A child that
inherits the full environment inherits `LD_PRELOAD`, `LD_LIBRARY_PATH`,
`PYTHONPATH`, `PYTHONHOME`, `BASH_ENV`, an `IFS` someone set and whatever
conda activated, plus a `PATH` with `.` in it — and the suite's `run` wrapper
is a `/bin/sh` script, so all of that is live.

`core/desmond/adapter.py::_worker_environment` builds the child's environment
from scratch rather than filtering the parent's: a fixed `PATH` of system
directories, `SCHRODINGER`, a temp `TMPDIR`, a `C.UTF-8` locale, headless Qt
and matplotlib, and exactly four variables forwarded from the parent
(`_FORWARDED_ENV`: `HOME`, `LANG`, and the two licence variables). Nothing
else crosses. `safety.minimal_env` is the shared primitive for any *other*
child process, built the same way — allowlist, absolute-only `PATH`,
`PYTHONNOUSERSITE=1`, `os.environ` never modified. Anything new that spawns a
process uses one of the two, never a bare `subprocess` call.

The captured streams are bounded: `CAPTURE_CAP_BYTES` (256 KiB, the same
number as `Limits.max_subprocess_output`) with `_truncate` saying plainly that
it cut, and both pipes drained by helper threads so a chatty child cannot
deadlock the reader.

### T11. The Schrodinger installation path

`$SCHRODINGER` is configuration the user supplies, and we execute programs
under it. We deliberately do not verify signatures or hashes of Schrodinger's
binaries: a user who points the tool at a hostile installation has been
compromised at a lower layer. What we do control is that the root is
recognised as a suite before use (`_looks_like_suite`, `_inspect`,
`discover_installations`), that the argv is a list with no shell anywhere, and
that the environment handed over is the one built in T10.

The generated `.sh` resolves `${SCHRODINGER}` at run time on the cluster,
under `set -euo pipefail` and `: "${SCHRODINGER:?...}"`, so an unset variable
fails loudly instead of executing `/utilities/multisim`.

### T12. Malformed input must produce a diagnostic, not a traceback

Every parser here is reached from a GUI import path. An `IndexError` from a
short line, a `UnicodeDecodeError` from a Latin-1 comment, a `ZeroDivisionError`
from a zero box vector: each is a crash that loses the user's unsaved work,
which is the outcome an accidental adversary produces most often.
`read_text_file` cannot raise `UnicodeDecodeError` — Latin-1 is a total
fallback — and every refusal in `safety` is one of four documented types
(`LimitExceeded`, `UnsafeArchive`, `ExternalChange`, `ValueError`) so a caller
can turn it into a diagnostic in the Check tab.

### T13. Silent corruption of a file that was already correct

The attack nobody defends against because it does not look like one: the tool
opens a working `.pot`, rewrites a BOM away, converts CRLF to LF, renormalises
a number from `1.0e-12` to `1e-12`, drops a comment it did not understand, and
saves. Nothing errors. The diff is enormous, the reviewer stops reading it,
and a real change hides in the noise. Worse: the file now differs from the one
the published run used.

This is treated as a security property because the impact is the same class as
any other integrity failure. `safety.Encoding` reports the file's identity and
reproduces it exactly (`enc.encode(enc.decode(raw)) == raw` is established at
sniff time, not assumed); the CST is a tree of *spans over the original text*
and an edit is a patch to a span (`core/lang/patch.py`), so anything the
interface does not model survives by construction; `core/potfile.py` re-emits
imported literals verbatim while a value is untouched; and export re-reads
what it wrote and verifies it against the model.

### T14. Untrusted input is handed to the vendor's parser on purpose

Official validation means giving Schrodinger's `enhsamp` the untrusted `.pot`
and the untrusted `.cms`. That is the point — our own reading of the language
is not authoritative — but it means a hostile file reaches a large parser we
did not write and cannot audit.

The containment is the process boundary, and it is deliberate:
`core/desmond/_worker.py` runs under the *suite's* interpreter, in a separate
process started with `start_new_session=True` so it owns its process group,
in a fresh temporary working directory that is removed afterwards, under the
scrubbed environment above, with a timeout that ends in `_kill_group` sending
`SIGTERM` then `SIGKILL` to the whole tree, and with its output capped and
parsed as one JSON object. The M-expression is never evaluated, compiled or
`exec`'d in either process; the worker hands it to the engine as an opaque
string. A crash, a hang or a fork bomb inside the engine costs a subprocess,
not the session.

---

## 5. Threat -> mitigation -> module -> test

Status is **in place** where the code exists today, **open** where the threat
is real and the fix is still section 7. The safety self-test named in the last
column is `python -m funnelforge.core.safety`; it currently runs 148 checks.

| # | Threat | Mitigation | Module | Status | Verified by |
| --- | --- | --- | --- | --- | --- |
| T1 | Code execution via `eval` of a `.pot` | Closed function table, explicit tree-walking interpreter, no `eval`/`exec`/`shell=True` in the package | `core/mexpr.py` | in place | `tests/test_funnelforge.py::test_evaluator_language_basics`, `::test_expression_static_checks`; the `grep` in T1 |
| T2 | Command injection through a job name reaching `SH_TEMPLATE` | Job-name whitelist at import and before templating; no shell for child processes | `core/jobfiles.py`, `core/project.py` | **open, see 7.1** | — |
| T3 | Path traversal on export | `canonical_path` on every IO path; `atomic_write` will not create the destination directory | `core/safety.py` | in place | safety self-test: "makes a relative path absolute", "rejects a non-existent destination directory" |
| T4 | ZIP path traversal (`..`, absolute, backslash, drive, UNC) | `_member_parts` under POSIX + Windows rules; destination-relative join; resolved-parent containment check | `core/safety.py` | in place | safety self-test: "absolute member", "'..' traversal", "nested '..' traversal", "backslash separator", "windows drive letter", "windows UNC path", "reserved windows device name", "component ending in a dot", "illegal windows character" |
| T4b | ZIP symlink and device members | `_reject_member_type`; `O_CREAT`, `O_EXCL` and `O_NOFOLLOW` on every output | `core/safety.py` | in place | safety self-test: "symlink member", "FIFO member", "character-device member", "member landing on a planted symlink", "member below a symlinked subdirectory" |
| T4c | Two members that collide on a case-insensitive filesystem | Case-insensitive duplicate check across the whole archive | `core/safety.py` | in place | safety self-test: "duplicate member names" |
| T5 | Zip bomb / decompression amplification | `max_archive_bytes`, `max_file_bytes`, `max_archive_members`, `max_archive_ratio`, plus a running total during extraction | `core/safety.py` | in place | safety self-test: "compression-ratio bomb", "too many members", "member over max_file_bytes", "total over max_archive_bytes", "archive file itself over max_archive_bytes" |
| T5b | Forged member sizes | Pre-checks on the central directory; CRC verification while streaming | `core/safety.py` | in place | safety self-test: "header understating a member's size" |
| T6 | Oversized file | `read_text_file` caps bytes read, not `st_size` | `core/safety.py` | in place | safety self-test: "refuses a file over max_file_bytes" |
| T6b | Deeply nested expression -> `RecursionError` | Explicit depth counter in the CST parser (256); bounded dependency walk in the analyser (512, `SEM004`) | `core/lang/cst.py`, `core/lang/sema.py`, `core/safety.py` | in place | safety self-test: "enforce_limit refuses over the cap"; `cst.parse(max_depth=...)`, `sema.analyse(max_dependency_depth=...)` |
| T6c | Token flood / statement flood | `tokenize(max_tokens=2_000_000)` refuses rather than continuing; `mexpr` caps series iterations at 200 000 | `core/lang/lexer.py`, `core/mexpr.py` | in place | `tests/test_funnelforge.py::test_expression_static_checks` |
| T7 | Symlink swap between validation and save | `os.replace` replaces the directory entry; `expect_digest` refuses a stale save; `canonical_path(allow_symlink=False)` where following a link is itself the risk | `core/safety.py` | in place | safety self-test: "a symlink at the destination is replaced, not followed", "detects an external change before writing", "refuses to create a file that already exists", "rejects a symlink anywhere in the chain" |
| T7b | Half-written file after a crash or a full disk | Sibling temp file, `flush` + `fsync`, `os.replace`, directory `fsync`; temp removed on any failure | `core/safety.py` | in place | safety self-test: "no temp files were left behind" |
| T8 | Leak through world-readable temp files | Temp file in the destination directory at `0o600`; new destinations `0o600`; existing modes preserved | `core/safety.py` | in place | safety self-test: "creates new files private", "preserves the destination's mode" |
| T8b | Leak through telemetry | No network code, no telemetry, no update check in the package; the worker's temp directory is its `TMPDIR` and is removed afterwards | whole package, `core/desmond/adapter.py` | in place | the network `grep` in T8 returns nothing |
| T9 | Out-of-range atom index from a `.cms` | Range check in `atomsel` against `n_atoms` | `core/mexpr.py`, `core/cms.py` | in place | `tests/test_funnelforge.py::test_evaluator_rejects_out_of_range_atoms` |
| T9b | Code execution via the pickled structure cache | Replace pickle with a versioned `.npz` + JSON sidecar | `core/cms.py` | **open, see 7.2** | — |
| T10 | Environment injection into `$SCHRODINGER/run` | Child environment built from scratch, four variables forwarded; `minimal_env` allowlist for every other child; no shell anywhere | `core/desmond/adapter.py`, `core/safety.py` | in place | safety self-test: "drops LD_PRELOAD", "drops PYTHONPATH", "drops BASH_ENV", "cleans PATH", "os.environ is untouched" |
| T10b | Validator floods stdout, or blocks on a full pipe | `CAPTURE_CAP_BYTES` with `_truncate`; both pipes drained in helper threads | `core/desmond/adapter.py` | in place | — |
| T12 | Malformed input crashes the GUI | Total decode (Latin-1 fallback), four documented refusal types, refusals surfaced as diagnostics rather than tracebacks | `core/safety.py`, `core/lang/diagnostics.py`, `core/project.py` | in place | safety self-test encoding matrix; `tests/test_funnelforge.py::test_validation_finds_planted_problems` |
| T13 | Silent corruption of a valid file (wrong science) | Byte-exact `Encoding` round trip; a CST of spans over the original text, edited by patching spans instead of regenerating; literals preserved through unrelated edits; re-read and verify after export | `core/safety.py`, `core/lang/cst.py`, `core/lang/patch.py`, `core/potfile.py`, `core/project.py` | in place | safety self-test encoding matrix (56 combinations: 7 codecs x 4 line-ending kinds x final newline); `tests/test_funnelforge.py::test_pot_round_trip_is_byte_identical`, `::test_pot_literals_survive_unrelated_edits` |
| T13b | A "valid" verdict that was never really checked | The adapter refuses to claim a pass without a topology (`NO_TOPOLOGY_REASON`), because the engine silently degrades to a workaround; every run carries a reproducible manifest | `core/desmond/adapter.py` | in place | — |
| T14 | Hostile file reaches the vendor's parser | Separate process under the suite's interpreter, own session and process group, temp workdir, timeout ending in `_kill_group`, scrubbed env, capped capture, JSON-only channel | `core/desmond/adapter.py`, `core/desmond/_worker.py` | in place | — |

The safety self-test is `python -m funnelforge.core.safety`; it builds the
malicious archives it needs in a temp directory and prints one line per check.
It lives in the module rather than in `tests/` so that it can be run on any
machine the application is installed on. When `tests/test_safety.py` is
added, it should assert the same labels.

---

## 6. Explicitly out of scope

These are decisions, not oversights. Each one would change the shape of the
product, and none of them is the attack that will actually happen.

* **A malicious local user, or malware already running as the user.** They can
  read the same files this application can. Nothing here is a sandbox.
* **The integrity of the Schrodinger installation.** We execute what is under
  `$SCHRODINGER`. Verifying a vendor's binaries is the operating system's job
  and the site's packaging policy, not ours.
* **Correctness of Desmond itself.** We validate that a `.pot` says what the
  model means; whether Desmond then integrates it correctly is out of our
  hands, and the official validator is used precisely because our reading of
  the language is not authoritative.
* **Physical/GPU side channels, timing attacks, memory scraping.** A design
  tool on a workstation is not the right place to spend that effort.
* **Multi-user, multi-tenant or networked operation.** There is no server, no
  daemon and no shared state between users. If that ever changes, this
  document is void until rewritten.
* **Encrypted archives.** `safe_extract_zip` refuses them rather than growing
  a password path; a run package should not be encrypted.
* **Preserving hostile-but-legal names on extraction.** A member called
  `aux.pot` or `data .cms` is refused even on Linux, where it would work. A
  package that extracts differently depending on the scientist's operating
  system is a bug even when it is not an attack.
* **Denial of service by a user against themselves.** Loading a 30 GB
  trajectory will be slow. The limits protect against files that are hostile,
  not against files that are big on purpose; ceilings are per-call arguments
  so an intentional large job can raise them.

---

## 7. Open items

### 7.1 Job names reach a shell script (T2) — `core/jobfiles.py`, `core/project.py`

`SH_TEMPLATE` interpolates `JOBNAME="{jobname}"` and `project.export` builds
file names by concatenation. The value can come from an imported `.sh` or from
a file stem, i.e. from untrusted input.

Fix: one whitelist, applied where the name is accepted (`import_bundle`,
`ShFile` parsing) and again where it is used (`ensure_job_files`, `export`,
`SH_TEMPLATE`): match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, reject anything
else with an `Issue` rather than silently sanitising, since a job name that
changes shape between import and export breaks the file-set consistency the
Check tab enforces. The same whitelist should gate the `description`, `host`
and `lic` fields, which are interpolated into the same template.

### 7.2 The structure cache is a pickle (T9b) — `core/cms.py`

`_cache_path` derives a path from `XDG_CACHE_HOME`; `pickle.load` on the
result is arbitrary code execution.

Fix: store the arrays with `numpy.savez` and the metadata as JSON in the same
file's sidecar, keyed by the same digest, and drop `pickle` entirely. Until
then the cache directory should at least be created `0o700` through
`safety.canonical_path(..., allow_symlink=False)` so that a link cannot point
it at a file someone else can write.

### 7.3 The ceilings are written twice (T6b, T6c, T10b)

`lang/lexer.tokenize(max_tokens=2_000_000)`, `lang/cst.parse(max_depth=256)`,
`lang/sema.analyse(max_dependency_depth=512)` and
`desmond/adapter.CAPTURE_CAP_BYTES = 256 * 1024` each repeat a number that
`safety.Limits` also holds. They agree today. They will not agree after the
first time somebody tunes one of them for a large system.

Fix: let each of those take its default from `DEFAULT_LIMITS`, keep the
parameter so a call site can still tighten it, and route the refusals through
`enforce_limit` so a user sees one wording. `Limits.max_statements` has no
enforcer at all yet; the analyser is its natural home.

### 7.4 `RecursionError` as a refusal type (T6b)

`lang/cst` raises `RecursionError("expression nesting limit")` at the depth
ceiling. That is a resource refusal wearing a Python built-in's name, and a
caller cannot distinguish it from a genuine interpreter stack overflow. It
should be `safety.LimitExceeded`, named and numbered like every other ceiling.

---

## 8. Rules for new code

1. Read a file through `safety.read_text_file`. Never `open(path).read()` on
   anything a user chose.
2. Write a file through `safety.atomic_write`, with the `Encoding` that
   `read_text_file` returned, and with `expect_digest` whenever the file was
   read earlier in the session.
3. Turn every user-supplied path into `safety.canonical_path` output before
   using it, and pass `allow_symlink=False` when following a link would change
   what you are allowed to touch.
4. Never call `zipfile.extract`, `tarfile.extractall`, or `shutil.unpack_archive`.
5. No `eval`, `exec`, `compile`, `pickle.load`, `os.system`, `shell=True`, or
   string-formatted shell commands. A child process is an argument list plus
   an environment built from scratch — `safety.minimal_env()`, or the
   adapter's `_worker_environment` when the child is the suite.
6. Every loop, recursion and allocation whose bound comes from a file takes
   its ceiling from `Limits` and refuses with `enforce_limit`.
7. A refusal is a diagnostic. Raise `LimitExceeded`, `UnsafeArchive`,
   `ExternalChange` or `ValueError` with a message naming the file and the
   number, and let the Check tab show it. Never `assert`, never a bare
   `except`, never a silent `pass`.
8. If a change makes any row of the table in section 5 false, update the row
   in the same commit.
