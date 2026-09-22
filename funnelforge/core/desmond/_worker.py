"""Out-of-process bridge to the installed Schrodinger enhanced-sampling parser.

This file is executed by the Schrodinger interpreter, never by the application
interpreter::

    <SCHRODINGER>/run .../_worker.py <cms-path|-> <pot-path>

It imports :mod:`schrodinger.application.desmond.enhsamp`, hands it the real
topology and the real M-expression, and prints exactly one JSON object on
standard output.  Nothing else may reach stdout, because the caller parses it.

Three properties of the engine force the shape of this script, and all three
were established by running it, not by reading the source:

* ``mexpParser.reportError`` writes ``line L:C <message>`` to file descriptor 2
  and then calls ``exit(1)``.  A syntax error therefore arrives as
  :class:`SystemExit`, so every handler here catches :class:`BaseException`;
  catching :class:`Exception` would let a mere typo kill the worker silently.
* ``enhsamp.resolve_atomsel`` calls ``os.dup2`` on ``sys.stdout.fileno()``.
  Redirecting with :class:`io.StringIO` makes it raise
  ``io.UnsupportedOperation: fileno``, so the capture below is done at the file
  descriptor level against a real temporary file.
* The interesting part of a syntax error is only in that descriptor-level
  output, never in the exception, so the captured text is shipped back too.

The M-expression is never evaluated, compiled, or exec'd here; it is handed to
the engine as an opaque string.

Deliberately written for Python 3.6: the user may point the adapter at an old
suite (Suite 2020-3 on this machine ships 3.6), and a worker that cannot even
be compiled there would report "validator crashed" for every file.  So there is
no ``from __future__ import annotations``, no f-string, no PEP 604 union, and
no dataclass in this file.
"""

import json
import os
import sys
import tempfile
import time
import traceback

#: Bumped whenever the JSON contract below changes.
WORKER_VERSION = "1.0.0"

EXIT_RAN = 0             # the engine ran; "ok" says whether it accepted the input
EXIT_USAGE = 2           # wrong argv
EXIT_IMPORT = 3          # the Schrodinger modules are not importable
EXIT_NO_TOPOLOGY = 4     # declined: official validation needs a topology
EXIT_BAD_TOPOLOGY = 5    # the topology could not be read
EXIT_NO_POTENTIAL = 6    # the .pot file could not be read

_TRACEBACK_CAP = 16 * 1024
_ENGINE_OUTPUT_CAP = 64 * 1024

#: Why a run without a .cms is refused rather than faked.  Established by
#: calling ``enhsamp.parseStr(None, text)`` directly: it constructs
#: ``ASLObject(None)``, which cannot ask a structure for gids and falls back to
#: a workaround that only understands a literal ``atom. <indices>`` selection
#: and assumes ``gid == atid - 1``.  Every other selection raises
#: ``RuntimeError("Failed to get gid from asl ('protein') without structure.")``
#: and the workaround is documented in enhsamp.py itself as wrong for FEP with
#: metadynamics and for molecules carrying virtual sites.  An "accepted" verdict
#: obtained that way would not mean the potential is valid for this system.
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


class _FdCapture:
    """Point file descriptors 1 and 2 at a temporary file for the duration.

    Descriptor-level rather than :mod:`io`-level because the engine dups the
    descriptors itself and because its parse errors are written to fd 2 by
    ANTLR, below anything Python-visible.

    Nothing may be printed while this is active: fd 1 is the temporary file, so
    a result emitted inside the block would be swallowed instead of reaching
    the caller.  :func:`main` therefore emits exactly once, after the block.
    :attr:`captured` holds everything the engine wrote and is filled in on the
    way out.
    """

    def __init__(self):
        self._file = tempfile.TemporaryFile(mode="w+b")
        self._saved_stdout = -1
        self._saved_stderr = -1
        self.captured = ""

    def __enter__(self):
        sys.stdout.flush()
        sys.stderr.flush()
        self._saved_stdout = os.dup(1)
        self._saved_stderr = os.dup(2)
        os.dup2(self._file.fileno(), 1)
        os.dup2(self._file.fileno(), 2)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except BaseException:
            pass
        os.dup2(self._saved_stdout, 1)
        os.dup2(self._saved_stderr, 2)
        os.close(self._saved_stdout)
        os.close(self._saved_stderr)
        self.captured = self._read()
        return False

    def _read(self):
        """Everything the engine wrote, decoded leniently and capped."""
        try:
            self._file.seek(0)
            raw = self._file.read(_ENGINE_OUTPUT_CAP + 1)
        except BaseException:
            return ""
        finally:
            try:
                self._file.close()
            except BaseException:
                pass
        text = raw.decode("utf-8", "replace")
        if len(raw) > _ENGINE_OUTPUT_CAP:
            text = text[:_ENGINE_OUTPUT_CAP] + "\n...[engine output truncated]..."
        return text


def _blank_result() -> dict:
    """The JSON contract, with every key always present."""
    return {
        "worker_version": WORKER_VERSION,
        "ok": False,
        "error": "",
        "etype": "",
        "traceback": "",
        "compiled_chars": 0,
        "schrodinger_version": None,
        "engine_output": "",
        "stage": "start",
        "read_cms_s": 0.0,
        "parse_s": 0.0,
        "python_version": "%d.%d.%d" % sys.version_info[:3],
    }


def _record_exception(result, exc):
    """Fill the failure half of the contract from a live exception."""
    result["etype"] = type(exc).__name__
    result["error"] = str(exc).strip()
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(text) > _TRACEBACK_CAP:
        text = text[:_TRACEBACK_CAP] + "\n...[traceback truncated]..."
    result["traceback"] = text


def _resolve_message(result):
    """Give a failure a usable message once the engine's output is known.

    ``str(SystemExit(1))`` is the useless string ``"1"``, and that is exactly
    how a syntax error arrives, because the parser writes the real complaint to
    the descriptor and then exits.  This runs after the capture has been read,
    so the complaint is available to stand in for the exit status.
    """
    if result["ok"] or not result["etype"]:
        return
    message = result["error"]
    if message and result["etype"] != "SystemExit":
        return
    lines = [ln.strip() for ln in result["engine_output"].splitlines() if ln.strip()]
    if lines:
        result["error"] = "\n".join(lines)
    elif not message:
        result["error"] = result["etype"]


def _emit(result, exit_code):
    """Print the single JSON object and return the process exit status.

    Only ever called with file descriptor 1 pointing at the real standard
    output; see :class:`_FdCapture`.
    """
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return exit_code


def main(argv):
    result = _blank_result()

    if len(argv) != 3:
        result["stage"] = "usage"
        result["etype"] = "Usage"
        result["error"] = "usage: _worker.py <cms-path|-> <pot-path>"
        return _emit(result, EXIT_USAGE)

    cms_arg, pot_path = argv[1], argv[2]

    result["stage"] = "read_potential"
    try:
        with open(pot_path, "r", encoding="utf-8", errors="replace") as handle:
            pot_text = handle.read()
    except BaseException as exc:
        _record_exception(result, exc)
        return _emit(result, EXIT_NO_POTENTIAL)

    # Everything below runs with fd 1 and fd 2 pointing at a temporary file, so
    # no branch in here may print: the exit status is carried out in a variable
    # and the one and only JSON object is written after the block.
    capture = _FdCapture()
    exit_code = EXIT_RAN
    with capture:
        try:
            result["stage"] = "import:schrodinger.application.desmond.enhsamp"
            from schrodinger.application.desmond import enhsamp

            result["stage"] = "import:schrodinger.application.desmond.packages.topo"
            from schrodinger.application.desmond.packages import topo
        except BaseException as exc:
            _record_exception(result, exc)
            result["error"] = "failed to import %s: %s" % (
                result["stage"].split(":", 1)[1], result["error"])
            exit_code = EXIT_IMPORT
        else:
            try:
                import schrodinger
                result["schrodinger_version"] = schrodinger.get_release_name()
            except BaseException:
                result["schrodinger_version"] = None

            if cms_arg == "-":
                result["stage"] = "topology"
                result["etype"] = "TopologyRequired"
                result["error"] = NO_TOPOLOGY_REASON
                exit_code = EXIT_NO_TOPOLOGY
            else:
                exit_code = _run_engine(result, enhsamp, topo, cms_arg, pot_text)

    result["engine_output"] = capture.captured
    _resolve_message(result)
    return _emit(result, exit_code)


def _run_engine(result, enhsamp, topo, cms_path, pot_text):
    """Read the topology and parse the potential. Returns the exit status.

    Runs with the descriptors captured, so it must not print either.
    """
    result["stage"] = "read_cms"
    started = time.time()
    try:
        _msys_model, cms_model = topo.read_cms(cms_path)
    except BaseException as exc:
        result["read_cms_s"] = time.time() - started
        _record_exception(result, exc)
        return EXIT_BAD_TOPOLOGY
    result["read_cms_s"] = time.time() - started

    result["stage"] = "parse"
    started = time.time()
    try:
        compiled = enhsamp.parseStr(cms_model, pot_text)
    except BaseException as exc:
        result["parse_s"] = time.time() - started
        _record_exception(result, exc)
        result["ok"] = False
    else:
        result["parse_s"] = time.time() - started
        result["ok"] = True
        result["compiled_chars"] = len(compiled)
    result["stage"] = "done"
    return EXIT_RAN


if __name__ == "__main__":
    sys.exit(main(sys.argv))
