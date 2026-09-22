"""Official Desmond validation: run the installed Schrodinger parser for real.

This module is the only place in FunnelForge allowed to say that a potential is
Desmond-valid.  Everything else in the tree - the CST, the semantic analyser,
:mod:`funnelforge.core.mexpr` - is a *model* of the language, useful for fast
feedback while typing but never authoritative.  Authority lives in
``schrodinger.application.desmond.enhsamp``, which we cannot import into this
process (it needs Schrodinger's own Python 3.11 and its compiled libraries), so
we drive it out of process through :mod:`._worker`.

The design follows from three things the engine actually does, all measured on
a live installation rather than inferred:

* a syntax error is reported by ANTLR to file descriptor 2 as
  ``line L:C <message>`` and then raises :class:`SystemExit`, so the useful text
  is descriptor output, not an exception;
* semantic errors are ordinary exceptions with good messages
  (``Number of arguments to dot must be in the set [2]``);
* without a topology the engine silently degrades to a workaround that only
  handles literal ``atom. <indices>`` selections and assumes ``gid == atid - 1``,
  so a pass without a ``.cms`` means nothing and this adapter refuses to claim
  one.  See :data:`NO_TOPOLOGY_REASON`.

Every run is described by a manifest - hashes of both inputs, the exact argv,
the installation, timings - so a verdict can be reproduced or disputed later.

The subprocess is started from an argument list with no shell anywhere, in a
fresh temporary directory that is removed afterwards, under a minimal
allow-listed environment, in its own process group so a timeout can kill the
whole tree.  The M-expression is never evaluated in this process.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

#: Bumped whenever the manifest or the verdict semantics change.
ADAPTER_VERSION = "1.0.0"

#: Per-stream capture limit.  Enough for any real engine message, small enough
#: that a runaway process cannot exhaust memory through the pipe.
CAPTURE_CAP_BYTES = 256 * 1024

_WORKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_worker.py")

NO_TOPOLOGY_REASON = (
    "official validation needs a topology. Without a .cms the engine builds "
    "ASLObject(None), which cannot resolve atom selections against a structure: "
    "it falls back to a workaround that understands only a literal "
    "\"atom. <indices>\" selection and assumes gid == atid - 1, and raises "
    "RuntimeError(\"Failed to get gid from asl ('...') without structure.\") for "
    "every other ASL. Schrodinger's own comment marks that workaround as wrong "
    "for FEP with metadynamics and for molecules with virtual sites, so a pass "
    "without a topology would not mean the potential is valid for this system. "
    "Supply the .cms the job will actually run against."
)


# --------------------------------------------------------------------------
# diagnostics, imported defensively
# --------------------------------------------------------------------------
# funnelforge.core.lang.diagnostics is written in parallel to the contract
# make(code, message, evidence=..., line=..., col=...).  Until it lands this
# module stays usable on its own with an equivalent local record.
try:                                                       # pragma: no cover
    from ..lang.diagnostics import make as _make_diagnostic
except ImportError:                                        # pragma: no cover
    _make_diagnostic = None


@dataclass(frozen=True)
class _FallbackDiagnostic:
    """Stand-in used only when ``funnelforge.core.lang.diagnostics`` is absent.

    Field-for-field compatible with the real record for the fields this module
    fills in, so downstream code does not have to know which one it is holding.
    """

    code: str
    message: str
    evidence: str = ""
    suggestion: str = ""
    line: int = 0
    col: int = 0
    data: dict = field(default_factory=dict)


def _diagnostic(code: str, message: str, evidence: str = "", line: int = 0,
                col: int = 0, data: dict | None = None, suggestion: str = ""):
    """Build one diagnostic through the shared factory when it exists."""
    payload = dict(data or {})
    if _make_diagnostic is not None:
        return _make_diagnostic(code, message, evidence=evidence, line=line,
                                col=col, data=payload, suggestion=suggestion)
    return _FallbackDiagnostic(code=code, message=message, evidence=evidence,
                               suggestion=suggestion, line=line, col=col,
                               data=payload)


#: Why the official layer said what it said.  The catalogue in
#: :mod:`funnelforge.core.lang.diagnostics` owns the ``OFF...`` ids and keeps
#: them deliberately coarse - accepted, rejected, not run, crashed - because
#: that is the distinction the UI has to draw.  The finer question of *what*
#: the engine objected to is still worth carrying, so it travels in the
#: diagnostic's ``data["reason"]`` using the vocabulary below.  Nothing here
#: was invented: every failure reason was produced by feeding the real parser
#: a broken potential and recording what came back.
OFFICIAL_REASONS: dict[str, str] = {
    "accepted": "the official parser accepted this potential",
    "warnings": "the official parser accepted it but printed warnings",
    "syntax": "the official parser could not parse the text",
    "unknown-function": "the official parser does not know this function",
    "unknown-variable": "the official parser does not know this variable",
    "arity": "wrong number of arguments",
    "type": "the official parser rejected the types",
    "duplicate-binding": "this name is bound twice in the same scope",
    "invalid-asl": "the atom selection is not valid ASL",
    "engine-error": "the official parser rejected this potential",
    "topology-required": "official validation needs a topology (.cms)",
    "topology-unreadable": "the topology could not be read",
    "no-installation": "no usable Schrodinger installation was found",
    "import-failed": "the Schrodinger Python modules could not be imported",
    "timed-out": "official validation timed out",
    "cancelled": "official validation was cancelled",
    "no-result": "the validation worker did not report a result",
}

#: Reason to catalogue id.  A rejection is only ever OFF003 when the engine
#: genuinely reached a verdict; everything that stopped us short of one is
#: "not performed" or "crashed", because reporting an unknown as a rejection
#: would tell the user their potential is broken when it may be fine.
_REASON_CODE: dict[str, str] = {
    "accepted": "OFF002",
    "warnings": "OFF010",
    "syntax": "OFF003",
    "unknown-function": "OFF003",
    "unknown-variable": "OFF003",
    "arity": "OFF003",
    "type": "OFF003",
    "duplicate-binding": "OFF003",
    "invalid-asl": "OFF003",
    "engine-error": "OFF003",
    "topology-required": "OFF001",
    "topology-unreadable": "OFF001",
    "no-installation": "OFF004",
    "import-failed": "OFF006",
    "timed-out": "OFF005",
    "cancelled": "OFF001",
    "no-result": "OFF006",
}

#: Releases whose behaviour this adapter has actually been exercised against.
#: A verdict from anything else is still authoritative - it is the engine, not
#: us - but the surrounding message parsing may be out of date, which is
#: exactly what OFF007 exists to say.
TESTED_RELEASES: frozenset = frozenset({"2025-3"})


# --------------------------------------------------------------------------
# installations
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Installation:
    """One Schrodinger suite on this machine.

    ``runner`` is the absolute path to the suite's ``run`` wrapper.  It accepts
    a ``.py`` script directly, so the worker is invoked as
    ``[runner, worker_path, cms_or_dash, pot_path]`` - see :meth:`argv_for`.
    ``usable`` means the wrapper and the ``schrodinger`` package are both on
    disk; it is never established by executing anything, because discovery runs
    on the GUI thread at start-up.
    """

    path: str
    version: str
    build: str
    runner: str
    usable: bool
    note: str = ""

    def argv_for(self, worker_path: str, cms_or_dash: str, pot_path: str) -> list[str]:
        """The exact argument list used to invoke the worker."""
        return [self.runner, worker_path, cms_or_dash, pot_path]

    @property
    def label(self) -> str:
        """Short human-facing name, e.g. ``2025-3 build 160``."""
        version = self.version or "unknown"
        return "%s build %s" % (version, self.build) if self.build else version


#: Where suites live, in the order the spec fixes.  ``$SCHRODINGER`` is handled
#: separately so a deliberately pointed-at suite always wins and is always
#: reported, even when it is broken.
_SEARCH_GLOBS: tuple = (
    "/opt/schrodinger*",
    "~/schrodinger*",
    "/usr/local/schrodinger*",
)

_VERSION_TXT_RE = re.compile(
    r"Suite\s+([0-9A-Za-z._+-]+?)\s*,\s*Build\s+([0-9A-Za-z._+-]+)", re.IGNORECASE)


def _read_version_txt(root: str) -> tuple:
    """Parse ``version.txt`` without executing anything. Returns (version, build)."""
    path = os.path.join(root, "version.txt")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(4096)
    except OSError:
        return ("", "")
    match = _VERSION_TXT_RE.search(text)
    if match:
        return (match.group(1), match.group(2))
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return (first, "")


def _looks_like_suite(root: str) -> bool:
    """Cheap filter so a home directory of scratch folders is not reported.

    A directory counts as a candidate if it carries any suite landmark; this
    keeps license-only stubs such as ``/opt/schrodinger`` visible (so the user
    is told why they are unusable) while excluding unrelated matches of the
    ``~/schrodinger*`` glob.
    """
    for landmark in ("run", "version.txt", "licenses"):
        if os.path.exists(os.path.join(root, landmark)):
            return True
    return bool(glob.glob(os.path.join(root, "mmshare-v*")))


def _inspect(root: str) -> Installation:
    """Describe one candidate directory. Pure filesystem inspection."""
    version, build = _read_version_txt(root)
    runner = os.path.join(root, "run")

    if not os.path.isdir(root):
        return Installation(root, version, build, runner, False, "not a directory")

    if not os.path.isfile(runner):
        return Installation(root, version, build, runner, False,
                            "no 'run' wrapper in this directory")
    if not os.access(runner, os.X_OK):
        return Installation(root, version, build, runner, False,
                            "'run' is not executable by this user")

    packages = glob.glob(os.path.join(root, "internal", "lib", "python3*",
                                      "site-packages", "schrodinger"))
    if not packages:
        return Installation(root, version, build, runner, False,
                            "no schrodinger Python package under internal/lib")

    return Installation(root, version, build, runner, True, "")


def discover_installations() -> list:
    """Every Schrodinger suite visible on this machine, best candidate first.

    ``$SCHRODINGER`` is reported first when it is set, whether or not it is
    usable, because a wrong value there is the single most common reason the
    official validator goes quiet.  Nothing is executed: this is called while
    the window is being built.
    """
    found: list = []
    seen: set = set()

    def add(path: str) -> None:
        real = os.path.realpath(os.path.expanduser(path))
        if real in seen:
            return
        seen.add(real)
        found.append(_inspect(real))

    env_root = os.environ.get("SCHRODINGER", "").strip()
    if env_root:
        add(env_root)

    candidates: list = []
    for pattern in _SEARCH_GLOBS:
        candidates.extend(glob.glob(os.path.expanduser(pattern)))
    for candidate in sorted(candidates):
        real = os.path.realpath(candidate)
        if os.path.isdir(real) and _looks_like_suite(real):
            add(real)

    return found


def _version_key(inst: Installation) -> tuple:
    """Sort key that puts the newest suite first: (2025, 3) beats (2020, 3)."""
    numbers = [int(part) for part in re.findall(r"\d+", inst.version)] or [-1]
    build = int(re.sub(r"\D", "", inst.build) or -1)
    return (tuple(numbers), build)


def default_installation():
    """The suite to validate with, or ``None`` when there is nothing usable.

    An explicit, usable ``$SCHRODINGER`` always wins; otherwise the newest
    usable suite does, because the .pot language only ever gains features.
    """
    installations = discover_installations()
    usable = [inst for inst in installations if inst.usable]
    if not usable:
        return None

    env_root = os.environ.get("SCHRODINGER", "").strip()
    if env_root:
        env_real = os.path.realpath(os.path.expanduser(env_root))
        for inst in usable:
            if inst.path == env_real:
                return inst

    return max(usable, key=_version_key)


# --------------------------------------------------------------------------
# the result
# --------------------------------------------------------------------------
@dataclass
class OfficialResult:
    """What the real engine said, and everything needed to reproduce it.

    ``ran`` and ``ok`` are deliberately separate: ``ran=False`` means we never
    got a verdict (no installation, no topology, killed), which is not the same
    as the engine rejecting the potential.  The GUI must not paint a missing
    installation red as if the user's expression were broken.
    """

    ran: bool = False
    ok: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error_message: str = ""
    line: int = 0
    col: int = 0
    duration_s: float = 0.0
    timed_out: bool = False
    manifest: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)

    @property
    def cancelled(self) -> bool:
        """True when the caller's cancel event stopped the run."""
        return bool(self.manifest.get("cancelled"))

    def summary(self) -> str:
        """One line for a status bar."""
        if self.ok:
            return "official parser: accepted (%.2f s)" % self.duration_s
        if not self.ran:
            return "official parser: not run - %s" % (self.error_message or "unavailable")
        return "official parser: rejected - %s" % self.error_message


# --------------------------------------------------------------------------
# message interpretation
# --------------------------------------------------------------------------
#: ANTLR's own prefix. The column is 0-based there and 1-based everywhere in
#: this application, so it is shifted on the way out.
_ANTLR_RE = re.compile(r"^line\s+(-?\d+):(-?\d+)\s+(.*)$")

#: Noise the engine appends after the real message.
_ANTLR_TRAILERS = ("unable to parse m-expression", "failed to parse m-expression")


def _antlr_positions(engine_output: str) -> list:
    """Every ``line L:C message`` the recogniser emitted, in order."""
    hits = []
    for raw in engine_output.splitlines():
        match = _ANTLR_RE.match(raw.strip())
        if match:
            line = int(match.group(1))
            col = int(match.group(2))
            hits.append((max(line, 0), col + 1 if col >= 0 else 0, match.group(3).strip()))
    return hits


def _tidy(message: str) -> str:
    """Collapse the engine's whitespace and drop its trailing noise."""
    kept = []
    for raw in message.splitlines():
        text = raw.strip()
        if not text or text.lower() in _ANTLR_TRAILERS:
            continue
        kept.append(text)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


#: Engine messages can be enormous - a rejected ASL is quoted back twice, with
#: every atom index - and the GUI puts ``error_message`` in a one-line label.
#: The full text is always still there in ``stdout`` and in the evidence.
MESSAGE_CAP_CHARS = 400


def _shorten(message: str, cap: int = MESSAGE_CAP_CHARS) -> str:
    """Cut an engine message to label length on a word boundary."""
    if len(message) <= cap:
        return message
    head = message[:cap].rsplit(" ", 1)[0].rstrip(" ,;")
    return "%s ... (%d characters in total)" % (head, len(message))


def _evidence(engine_output: str, traceback_text: str) -> str:
    """The most informative few lines to show under the message.

    For a syntax error that is what ANTLR printed.  For an exception it is the
    deepest frame inside the engine plus the exception line - a slice out of
    the middle of a traceback tells the reader nothing.
    """
    captured = _tidy(engine_output)
    if captured:
        return _truncate(captured, 4096)

    lines = [ln.rstrip() for ln in traceback_text.splitlines() if ln.strip()]
    if not lines:
        return ""
    frames = [ln.strip() for ln in lines if ln.strip().startswith('File "')]
    parts = []
    if frames:
        parts.append(frames[-1])
    parts.append(lines[-1].strip())
    return _truncate("\n".join(parts), 4096)


def _unwrap_key_error(message: str) -> str:
    """``KeyError`` stringifies to ``"'name'"``; give back ``name``."""
    text = message.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _classify(etype: str, error: str, engine_output: str) -> tuple:
    """Map an engine failure onto (reason, message, line, col).

    Every branch here was produced by feeding the real parser a broken
    potential and recording what came back; nothing is guessed from the source.
    A syntax error is recognised first because it arrives as :class:`SystemExit`
    with the real text on the captured descriptor, which no later branch would
    make sense of.
    """
    positions = _antlr_positions(engine_output)
    if positions or etype == "SystemExit":
        if positions:
            line, col, text = positions[0]
            return ("syntax", _shorten(text), line, col)
        return ("syntax",
                _shorten(_tidy(error)) or "the expression could not be parsed", 0, 0)

    message = _shorten(_tidy(error))

    if etype in ("ImportError", "ModuleNotFoundError"):
        return ("import-failed", message, 0, 0)

    if etype == "TopologyRequired":
        return ("topology-required", message, 0, 0)

    if etype == "KeyError":
        # env.sigs[name] misses for a function; the variable lookup raises
        # KeyError with a sentence in it instead of a bare name.
        name = _unwrap_key_error(message)
        lowered = name.lower()
        if lowered.startswith("variable ") and lowered.endswith(" unknown"):
            return ("unknown-variable", "unknown variable '%s'" % name.split()[1], 0, 0)
        return ("unknown-function", "unknown function '%s'" % name, 0, 0)

    if "not a valid ASL expression" in error or etype == "MmException":
        return ("invalid-asl", message, 0, 0)

    if etype == "RuntimeError" and "without structure" in error:
        return ("topology-required", message, 0, 0)

    if etype == "TypeError":
        if "Number of arguments" in error or "takes one argument" in error:
            return ("arity", message, 0, 0)
        return ("type", message, 0, 0)

    if etype == "ValueError":
        lowered = error.lower()
        if "declared twice" in lowered or "bound twice" in lowered:
            return ("duplicate-binding", message, 0, 0)
        if "unkown variable" in lowered or "unknown variable" in lowered:
            return ("unknown-variable", message, 0, 0)
        return ("engine-error", message, 0, 0)

    return ("engine-error",
            message or ("%s from the official parser" % (etype or "error")), 0, 0)


# --------------------------------------------------------------------------
# process plumbing
# --------------------------------------------------------------------------
def _truncate(text: str, cap: int = CAPTURE_CAP_BYTES) -> str:
    """Cap a captured stream, saying plainly that it was cut."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= cap:
        return text
    head = raw[:cap].decode("utf-8", "ignore")
    return "%s\n...[truncated: %d of %d bytes shown]..." % (head, cap, len(raw))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> str:
    """Chunked so a 50 MB .cms does not land in memory twice."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: Variables forwarded from the parent, and only these.  Schrodinger's ``run``
#: wrapper is a /bin/sh script that shells out to uname/sed/ls, so PATH must
#: hold the system binaries; licensing reads HOME and the two license
#: variables.  Nothing else is inherited, so a stray PYTHONPATH, PYTHONHOME,
#: LD_LIBRARY_PATH or conda activation cannot reach into the suite.
_FORWARDED_ENV: tuple = ("HOME", "LANG", "SCHROD_LICENSE_FILE", "LM_LICENSE_FILE")


def _worker_environment(inst: Installation, workdir: str) -> dict:
    """The minimal environment the suite needs, built from scratch."""
    env = {
        "SCHRODINGER": inst.path,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.path.expanduser("~"),
        "TMPDIR": workdir,
        "TMP": workdir,
        "TEMP": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        # headless: no X server here, and Qt/matplotlib must not look for one
        "QT_QPA_PLATFORM": "offscreen",
        "MPLBACKEND": "Agg",
    }
    for name in _FORWARDED_ENV:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate the whole process group, then make sure of it.

    The child is started with ``start_new_session=True``, so its pgid is its
    pid and killing the group also collects anything the suite forked.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            continue


def _drain(stream, chunks: list) -> None:
    """Read one pipe to EOF in a helper thread so neither can ever block."""
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except (ValueError, OSError):
        pass
    finally:
        try:
            stream.close()
        except (ValueError, OSError):
            pass


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

    out_chunks: list = []
    err_chunks: list = []
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out_chunks), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_chunks), daemon=True),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + max(timeout, 0.0)
    timed_out = False
    cancelled = False
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.01)

    if timed_out or cancelled:
        _kill_group(proc)

    for reader in readers:
        reader.join(timeout=5.0)
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        _kill_group(proc)

    stdout = b"".join(out_chunks).decode("utf-8", "replace")
    stderr = b"".join(err_chunks).decode("utf-8", "replace")
    return (proc.returncode, stdout, stderr, timed_out, cancelled)


def _extract_json(stdout: str):
    """Pull the worker's single JSON object out of stdout.

    The suite's ``run`` wrapper is entitled to print its own lines, so the
    whole stream is tried first and then each line from the end, rather than
    assuming stdout is nothing but our object.
    """
    text = stdout.strip()
    if not text:
        return None
    for candidate in [text] + [ln.strip() for ln in reversed(text.splitlines())]:
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            return parsed
    return None


def _canonical_cms(cms_path: str) -> tuple:
    """Canonicalise and vet the topology path. Returns (real_path, error).

    ``realpath`` resolves ``..`` and every symlink, so a path crafted to escape
    a directory becomes the thing it actually points at; that target must then
    be a plain regular file.  A symlink to a fifo, a device or a directory is
    refused rather than handed to a subprocess.
    """
    real = os.path.realpath(os.path.expanduser(cms_path))
    if not os.path.exists(real):
        return ("", "topology not found: %s" % real)
    try:
        info = os.stat(real)
    except OSError as exc:
        return ("", "topology cannot be inspected: %s (%s)" % (real, exc.strerror))
    if not stat.S_ISREG(info.st_mode):
        return ("", "topology is not a regular file: %s" % real)
    if not os.access(real, os.R_OK):
        return ("", "topology is not readable: %s" % real)
    return (real, "")


# --------------------------------------------------------------------------
# the entry point
# --------------------------------------------------------------------------
def _manifest(pot_text: str, cms_real, inst, argv: list, exit_code,
              timed_out: bool, cancelled: bool, duration: float,
              started_utc: str, cms_sha) -> dict:
    """Everything needed to re-run this validation and get the same answer."""
    return {
        "pot_sha256": _sha256_text(pot_text),
        "pot_bytes": len(pot_text.encode("utf-8")),
        "cms_sha256": cms_sha,
        "cms_path": cms_real,
        "schrodinger_path": inst.path if inst is not None else None,
        "version": inst.version if inst is not None else None,
        "build": inst.build if inst is not None else None,
        "runner": inst.runner if inst is not None else None,
        "argv": list(argv),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "timestamp_utc": started_utc,
        "duration_s": round(duration, 4),
        "adapter_version": ADAPTER_VERSION,
    }


def _reason_diagnostic(reason: str, message: str, evidence: str = "",
                       line: int = 0, col: int = 0):
    """One diagnostic carrying both the catalogue verdict and the fine reason."""
    return _diagnostic(_REASON_CODE[reason], message, evidence=evidence,
                       line=line, col=col,
                       data={"reason": reason, "layer": "official"})


def _release_diagnostics(inst) -> list:
    """OFF007 when the suite is one this adapter has not been exercised against."""
    if inst is None or not inst.version or inst.version in TESTED_RELEASES:
        return []
    return [_diagnostic(
        "OFF007",
        "Desmond release %s has not been tested against this interface; its "
        "verdict still stands, but the way its messages are read here may be "
        "out of date" % inst.label,
        data={"reason": "untested-release", "layer": "official",
              "version": inst.version, "build": inst.build})]


def _refuse(reason: str, message: str, pot_text: str, cms_real, inst,
            started_utc: str, duration: float, cms_sha=None) -> OfficialResult:
    """Build a ``ran=False`` result: we never reached a verdict."""
    return OfficialResult(
        ran=False,
        ok=False,
        exit_code=None,
        stdout="",
        stderr="",
        error_message=message,
        line=0,
        col=0,
        duration_s=duration,
        timed_out=False,
        manifest=_manifest(pot_text, cms_real, inst, [], None, False, False,
                           duration, started_utc, cms_sha),
        diagnostics=[_reason_diagnostic(reason, message)],
    )


def validate(pot_text: str, cms_path: str | None = None, *,
             installation: Installation | None = None,
             timeout: float = 180.0,
             cancel: "threading.Event | None" = None) -> OfficialResult:
    """Ask the installed Desmond parser whether ``pot_text`` is valid.

    ``cms_path`` is required: see :data:`NO_TOPOLOGY_REASON` for why a verdict
    without a topology would be worthless.  The call blocks for roughly the
    time the suite needs to read the topology (about a second for a 50 MB
    system), so the GUI must run it off the main thread and may abort it
    through ``cancel``.
    """
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()

    inst = installation if installation is not None else default_installation()
    if inst is None:
        found = discover_installations()
        detail = "; ".join("%s (%s)" % (i.path, i.note or "unusable") for i in found)
        message = ("no usable Schrodinger installation was found. Set $SCHRODINGER "
                   "to a suite directory containing 'run'.")
        if detail:
            message += " Looked at: " + detail
        return _refuse("no-installation", message, pot_text, None, None,
                       started_utc, time.monotonic() - started)
    if not inst.usable:
        message = "Schrodinger installation at %s cannot be used: %s" % (
            inst.path, inst.note or "unusable")
        return _refuse("no-installation", message, pot_text, None, inst,
                       started_utc, time.monotonic() - started)

    if cms_path is None:
        return _refuse("topology-required", NO_TOPOLOGY_REASON, pot_text, None,
                       inst, started_utc, time.monotonic() - started)

    cms_real, cms_error = _canonical_cms(cms_path)
    if cms_error:
        return _refuse("topology-unreadable", cms_error, pot_text, None, inst,
                       started_utc, time.monotonic() - started)

    if cancel is not None and cancel.is_set():
        return _refuse("cancelled", "cancelled before the engine was started",
                       pot_text, cms_real, inst, started_utc,
                       time.monotonic() - started)

    try:
        cms_sha = _sha256_file(cms_real)
    except OSError as exc:
        return _refuse("topology-unreadable", "topology could not be read: %s (%s)" % (
            cms_real, exc.strerror), pot_text, cms_real, inst,
            started_utc, time.monotonic() - started)

    workdir = tempfile.mkdtemp(prefix="funnelforge-official-")
    try:
        pot_file = os.path.join(workdir, "potential.pot")
        with open(pot_file, "w", encoding="utf-8", newline="") as handle:
            handle.write(pot_text)

        argv = inst.argv_for(_WORKER_PATH, cms_real, pot_file)
        env = _worker_environment(inst, workdir)
        exit_code, stdout, stderr, timed_out, cancelled = _spawn(
            argv, env, workdir, timeout, cancel)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    duration = time.monotonic() - started
    payload = _extract_json(stdout)
    manifest = _manifest(pot_text, cms_real, inst, argv, exit_code, timed_out,
                         cancelled, duration, started_utc, cms_sha)

    result = OfficialResult(
        ran=False,
        ok=False,
        exit_code=exit_code,
        stdout=_truncate(stdout),
        stderr=_truncate(stderr),
        duration_s=duration,
        timed_out=timed_out,
        manifest=manifest,
    )
    if payload is not None:
        manifest["worker_version"] = payload.get("worker_version")
        manifest["schrodinger_release"] = payload.get("schrodinger_version")
        manifest["read_cms_s"] = payload.get("read_cms_s")
        manifest["parse_s"] = payload.get("parse_s")

    if timed_out:
        result.error_message = (
            "official validation timed out after %.3f s and the process group "
            "was killed" % timeout)
        result.diagnostics = [_reason_diagnostic(
            "timed-out", result.error_message, evidence=_truncate(stderr, 2048))]
        return result

    if cancelled:
        result.error_message = "official validation was cancelled by the caller"
        result.diagnostics = [_reason_diagnostic("cancelled", result.error_message)]
        return result

    if payload is None:
        result.error_message = (
            "the validation worker produced no JSON result (exit code %s). "
            "stderr: %s" % (exit_code, _tidy(stderr)[:400] or "(empty)"))
        result.diagnostics = [_reason_diagnostic(
            "no-result", result.error_message, evidence=_truncate(stderr, 2048))]
        return result

    engine_output = payload.get("engine_output", "") or ""
    etype = payload.get("etype", "") or ""
    error = payload.get("error", "") or ""

    if payload.get("ok"):
        result.ran = True
        result.ok = True
        result.error_message = ""
        manifest["compiled_chars"] = payload.get("compiled_chars", 0)
        compiled = payload.get("compiled_chars", 0)
        result.diagnostics = [_reason_diagnostic(
            "accepted",
            "accepted by the official Desmond parser (%s); it produced %d "
            "characters of backend configuration" % (inst.label, compiled))]
        # The engine prints its own warnings to the descriptor rather than
        # raising, so acceptance and a complaint can arrive together.
        if _tidy(engine_output):
            result.diagnostics.append(_reason_diagnostic(
                "warnings",
                "the parser accepted the potential but also printed: %s"
                % _shorten(_tidy(engine_output)),
                evidence=_truncate(engine_output, 4096)))
        result.diagnostics.extend(_release_diagnostics(inst))
        return result

    reason, message, line, col = _classify(etype, error, engine_output)
    # The worker says how far it got.  A failure while reading the topology is
    # a problem with the .cms, not a verdict on the potential, and the engine's
    # message for it ("Failed opening MAE file at ...") would otherwise be
    # classified as a rejection and told to the user as if their expression
    # were at fault.
    stage = payload.get("stage", "")
    if stage == "read_cms":
        reason = "topology-unreadable"
        message = "the topology could not be read: %s" % message
    elif stage == "read_potential":
        reason = "no-result"
        message = "the worker could not read the potential it was handed: %s" % message
    # An import failure or a declined run is not a verdict on the potential:
    # the engine never got as far as judging it, and saying otherwise would
    # tell the user their expression is broken when it may be perfect.
    result.ran = reason not in ("import-failed", "topology-required",
                                "topology-unreadable")
    result.ok = False
    result.error_message = message
    result.line = line
    result.col = col
    evidence = _evidence(engine_output, payload.get("traceback", "") or "")
    result.diagnostics = [_reason_diagnostic(reason, message, evidence=evidence,
                                             line=line, col=col)]
    if result.ran:
        result.diagnostics.extend(_release_diagnostics(inst))
    return result
