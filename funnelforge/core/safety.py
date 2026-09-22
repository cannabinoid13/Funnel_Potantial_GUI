"""Shared safety primitives: bounded reads, exact encodings, atomic writes.

Every file this application opens came from somewhere else - a ``.pot`` a
collaborator mailed, a ``.cms`` a workflow produced on a cluster, a ZIP of a
finished run someone downloaded.  None of it is trusted, and all of it is
parsed by code that is happy to recurse, allocate and iterate as deeply as the
input tells it to.  This module is the one place where that stops.

Three rules the layers above inherit from here:

* **Bounded.**  Nothing is read, extracted or counted without a ceiling from
  :class:`Limits`, and every ceiling is a field so a caller can tighten it for
  a particular source.  Hitting one raises :class:`LimitExceeded`, which is a
  diagnosable refusal, not a crash three frames deep in a parser.
* **Exact.**  A file's byte-level identity - BOM, codec, line-ending
  convention, whether it ends with a newline - is *detected and reported*,
  never quietly normalised.  :class:`Encoding` carries that identity, and
  ``enc.encode(enc.decode(raw)) == raw`` holds for every file
  :func:`read_text_file` accepts.  The importer must be able to re-emit an
  untouched file byte for byte, and a user whose file is CRLF or has no final
  newline must be told rather than have it silently rewritten under them.
* **Atomic.**  A save either lands whole or does not happen.
  :func:`atomic_write` writes a sibling temp file, fsyncs it, then
  :func:`os.replace`\\ s it into place, optionally refusing to run at all if
  the destination changed since the caller last read it
  (:class:`ExternalChange`).  ``os.replace`` swaps a directory entry, so a
  symlink planted at the destination between validation and save is replaced,
  not written through - the classic TOCTOU swap cannot redirect a save.

:func:`safe_extract_zip` applies all three to run packages, and
:func:`minimal_env` builds the scrubbed environment used for any child
process, because a ``.pot`` file name is attacker-influenced text that will
one day end up on a command line.

Portability
-----------
Path handling uses :mod:`os.path` and, for archive member names, both
:mod:`posixpath` and :mod:`ntpath`, so Windows path semantics are enforced
even while running on Linux: a member is rejected if it is absolute under
*either* convention, contains a backslash, carries a drive letter, names a
reserved device (``CON``, ``LPT1``, ...), ends a component with a space or a
dot, or uses a character Windows forbids.  Developed and exercised on Linux
only; these are the behaviours that were *not* run here and are written to be
correct by construction:

* ``os.replace`` onto an open destination fails on Windows with
  :class:`PermissionError` (POSIX allows it).  Callers that keep a handle on
  the file they are saving must close it first.
* fsyncing the containing directory after the replace is a POSIX durability
  step; :func:`os.open` on a directory fails on Windows, so it is skipped
  there (NTFS journals the rename itself).
* ``O_NOFOLLOW`` does not exist on Windows; the extractor falls back to
  ``O_EXCL`` alone, which still refuses to write onto an existing name.
* :func:`minimal_env` keeps a different variable set on Windows and
  upper-cases keys, because the Windows environment block is
  case-insensitive.
"""

from __future__ import annotations

import codecs
import dataclasses
import hashlib
import ntpath
import os
import posixpath
import stat
import tempfile
import zipfile
from dataclasses import dataclass


class LimitExceeded(Exception):
    """Untrusted input asked for more than a :class:`Limits` ceiling allows."""


class ExternalChange(Exception):
    """The destination file changed on disk since the caller last read it."""


class UnsafeArchive(ValueError):
    """An archive member is structurally hostile, not merely too large.

    Subclasses :class:`ValueError` so that callers which only care that the
    archive was rejected can keep a single ``except (ValueError,
    LimitExceeded)``.
    """


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Limits:
    """Ceilings applied to one untrusted source.

    The defaults are generous for real Desmond work and small enough that a
    hostile file cannot exhaust a workstation: the largest ``.pot`` in the
    wild is a few hundred kilobytes, and a solvated ``.cms`` for a million
    atoms is well under 32 MB.  Tighten them per call site rather than
    loosening them globally.
    """

    max_file_bytes: int = 32 * 1024 * 1024
    max_tokens: int = 2_000_000
    max_ast_depth: int = 256
    max_dependency_depth: int = 512
    max_statements: int = 200_000
    max_subprocess_output: int = 256 * 1024
    max_archive_bytes: int = 512 * 1024 * 1024
    max_archive_members: int = 10_000
    max_archive_ratio: float = 200.0      # zip-bomb guard

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"Limits.{f.name} must be a number")
            if value <= 0:
                raise ValueError(f"Limits.{f.name} must be positive, "
                                 f"got {value!r}")
        if self.max_archive_ratio < 1.0:
            raise ValueError("Limits.max_archive_ratio must be at least 1.0 "
                             "(a ratio below 1 rejects every compressed file)")


DEFAULT_LIMITS = Limits()


def enforce_limit(what: str, value: float, cap: float) -> None:
    """Raise :class:`LimitExceeded` when ``value`` exceeds ``cap``.

    Centralised so every layer refuses in the same words; the message names
    the quantity and both numbers, which is what a user needs in order to
    decide whether to raise the ceiling or distrust the file.
    """
    if value > cap:
        raise LimitExceeded(f"{what}: {value:g} exceeds the limit of {cap:g}")


# --------------------------------------------------------------------------
# encodings
# --------------------------------------------------------------------------
#: name -> (BOM bytes, codec used for the body after the BOM).
#: Splitting the BOM out of the codec keeps encode() an exact inverse of
#: decode(): the BOM is re-emitted verbatim rather than left to a codec that
#: might or might not add one.
_CODECS: dict[str, tuple[bytes, str]] = {
    "utf-8": (b"", "utf-8"),
    "utf-8-sig": (codecs.BOM_UTF8, "utf-8"),
    "utf-16-le": (codecs.BOM_UTF16_LE, "utf-16-le"),
    "utf-16-be": (codecs.BOM_UTF16_BE, "utf-16-be"),
    "utf-32-le": (codecs.BOM_UTF32_LE, "utf-32-le"),
    "utf-32-be": (codecs.BOM_UTF32_BE, "utf-32-be"),
    "latin-1": (b"", "latin-1"),
}

#: Longest BOM first: the UTF-32-LE mark starts with the UTF-16-LE mark.
_BOM_ORDER = ("utf-32-le", "utf-32-be", "utf-8-sig", "utf-16-le", "utf-16-be")

NEWLINE_KINDS = ("lf", "crlf", "cr", "mixed")


def _to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class Encoding:
    """The byte-level identity of a text file.

    ``name`` is a key of :data:`_CODECS`; ``bom`` says whether the file starts
    with a byte-order mark (implied by ``name``, kept explicit because the UI
    shows it); ``newline`` is the file's line-ending convention and
    ``final_newline`` whether the last line is terminated.

    :meth:`decode` translates ``crlf`` and ``cr`` files to ``\\n`` so the rest
    of the application only ever sees LF, and :meth:`encode` puts the file's
    own convention back.  A ``mixed`` file is passed through untouched in both
    directions: there is no rule that could restore the original mixture, so
    the honest thing is to keep the bytes and let the UI warn.  ``encode``
    never adds or removes a final newline - ``final_newline`` is reported so a
    writer can preserve the property deliberately.
    """

    name: str
    bom: bool
    newline: str
    final_newline: bool

    def __post_init__(self) -> None:
        if self.name not in _CODECS:
            raise ValueError(f"unknown encoding {self.name!r}")
        if self.bom != bool(_CODECS[self.name][0]):
            raise ValueError(f"encoding {self.name!r} cannot have bom="
                             f"{self.bom!r}")
        if self.newline not in NEWLINE_KINDS:
            raise ValueError(f"unknown newline kind {self.newline!r}")

    # -- description for the UI and for diagnostics
    def describe(self) -> str:
        parts = [self.name]
        if self.bom:
            parts.append("BOM")
        parts.append({"lf": "LF", "crlf": "CRLF", "cr": "CR",
                      "mixed": "mixed line endings"}[self.newline])
        if not self.final_newline:
            parts.append("no final newline")
        return ", ".join(parts)

    def decode(self, raw: bytes) -> str:
        """Bytes -> text, with the file's line endings normalised to LF."""
        bom_bytes, codec = _CODECS[self.name]
        if bom_bytes:
            if not raw.startswith(bom_bytes):
                raise ValueError(f"{self.name} expects a byte-order mark; "
                                 "this Encoding belongs to another file")
            raw = raw[len(bom_bytes):]
        text = raw.decode(codec)
        if self.newline == "crlf":
            return text.replace("\r\n", "\n")
        if self.newline == "cr":
            return text.replace("\r", "\n")
        return text

    def encode(self, text: str) -> bytes:
        """Text -> bytes, exactly inverting :meth:`decode`.

        Any line ending in ``text`` is written in the file's convention, so a
        block pasted in from a Windows editor does not turn an LF file into a
        mixed one.  Raises :class:`UnicodeEncodeError` if ``text`` contains a
        character the file's codec cannot hold (a non-Latin-1 character in a
        Latin-1 file, say) - that is a real conflict for the caller to
        resolve, not something to paper over with a replacement character.
        """
        if self.newline == "crlf":
            text = _to_lf(text).replace("\n", "\r\n")
        elif self.newline == "cr":
            text = _to_lf(text).replace("\n", "\r")
        elif self.newline == "lf":
            text = _to_lf(text)
        bom_bytes, codec = _CODECS[self.name]
        return bom_bytes + text.encode(codec)


DEFAULT_ENCODING = Encoding("utf-8", False, "lf", True)


def _classify_newlines(text: str) -> str:
    crlf = text.count("\r\n")
    cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    kinds = [k for k, n in (("crlf", crlf), ("cr", cr), ("lf", lf)) if n]
    if not kinds:
        return "lf"                     # no line ending at all; LF if we add one
    if len(kinds) == 1:
        return kinds[0]
    return "mixed"


def _candidates(raw: bytes) -> list[str]:
    """Codecs to try, most specific first; ``latin-1`` always ends the list."""
    # _BOM_ORDER is longest mark first, so the first hit is the specific one.
    names = [n for n in _BOM_ORDER if raw.startswith(_CODECS[n][0])][:1]
    names += ["utf-8", "latin-1"]
    return names


def sniff_encoding(raw: bytes) -> Encoding:
    """Detect the encoding of ``raw`` without ever failing.

    A byte-order mark decides the codec outright; otherwise UTF-8 is tried
    strictly and Latin-1 catches whatever is left, because Latin-1 maps every
    one of the 256 byte values to a distinct character and therefore cannot
    fail and cannot lose a byte.  Each candidate is accepted only once the
    resulting :class:`Encoding` has been proved to round-trip these exact
    bytes, so the invariant the rest of the codebase relies on
    (``enc.encode(enc.decode(raw)) == raw``) is established here rather than
    assumed.
    """
    raw = bytes(raw)
    for name in _candidates(raw):
        bom_bytes, codec = _CODECS[name]
        try:
            body = raw[len(bom_bytes):].decode(codec)
        except UnicodeDecodeError:
            continue
        enc = Encoding(name=name, bom=bool(bom_bytes),
                       newline=_classify_newlines(body),
                       final_newline=body.endswith(("\n", "\r")))
        if enc.encode(enc.decode(raw)) == raw:
            return enc
    raise AssertionError("latin-1 failed to round-trip; this cannot happen")


def read_text_file(path: str | os.PathLike,
                   limits: Limits = DEFAULT_LIMITS) -> tuple[str, Encoding]:
    """Read a text file with a hard size cap; return ``(text, encoding)``.

    The cap is applied to the bytes actually read, not to the size reported by
    :func:`os.stat`, so a file that grows between the stat and the read - or a
    pseudo-file that reports zero and streams forever - is still refused.  The
    returned text uses LF endings unless the file mixes conventions; the
    returned :class:`Encoding` reproduces the original bytes from it.
    """
    target = canonical_path(path, must_exist=True, must_be_file=True)
    cap = int(limits.max_file_bytes)
    with open(target, "rb") as fh:
        raw = fh.read(cap + 1)
    if len(raw) > cap:
        raise LimitExceeded(f"{target}: file is larger than the limit of "
                            f"{cap} bytes")
    enc = sniff_encoding(raw)
    return enc.decode(raw), enc


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
def _symlink_component(path: str) -> str | None:
    """The outermost symlink on ``path``, or ``None``.

    Walks upwards rather than downwards so that a link anywhere in the chain
    is found, which is what matters for a directory that was swapped after a
    file inside it was validated.
    """
    cur = path
    while True:
        if os.path.islink(cur):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def canonical_path(p: str | os.PathLike, *, must_exist: bool = False,
                   must_be_file: bool = False,
                   allow_symlink: bool = True) -> str:
    """Absolute, normalised, checked path as a string.

    ``~`` is expanded and the path is made absolute against the current
    directory, but symlinks are deliberately *not* resolved: the application
    should keep showing the user the path they chose, and a save through
    :func:`atomic_write` replaces a link rather than following it, so
    resolving here would only hide what is going on.  Pass
    ``allow_symlink=False`` where following a link would be a privilege
    change - then any link in the whole chain, not just the last component,
    is a refusal.

    Raises :class:`ValueError` for a malformed path or a wrong file type and
    :class:`FileNotFoundError` when ``must_exist`` is not met, so a caller can
    tell "you gave me nonsense" from "it is not there".
    """
    try:
        raw = os.fspath(p)
    except TypeError as exc:
        raise ValueError(f"not a path: {p!r}") from exc
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if not raw or not raw.strip():
        raise ValueError("empty path")
    if "\x00" in raw:
        raise ValueError("path contains a NUL byte")
    absolute = os.path.abspath(os.path.expanduser(raw))
    if not allow_symlink:
        link = _symlink_component(absolute)
        if link is not None:
            raise ValueError(f"{absolute}: refusing to follow the symbolic "
                             f"link at {link}")
    if must_exist or must_be_file:
        if not os.path.exists(absolute):
            raise FileNotFoundError(absolute)
    if must_be_file and not os.path.isfile(absolute):
        raise ValueError(f"{absolute}: not a regular file")
    return absolute


# --------------------------------------------------------------------------
# digests
# --------------------------------------------------------------------------
_CHUNK = 1 << 20


def file_digest(path: str | os.PathLike) -> str:
    """SHA-256 of a file's bytes as hex, or ``""`` when it does not exist.

    The empty string is a real value here, not an error: it means "nothing is
    there", which is exactly the precondition a caller creating a new file
    wants to assert through :func:`atomic_write`'s ``expect_digest``.
    """
    h = hashlib.sha256()
    try:
        fh = open(os.fspath(path), "rb")
    except (FileNotFoundError, NotADirectoryError):
        return ""
    with fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def text_digest(text: str) -> str:
    """SHA-256 of ``text`` encoded as UTF-8.

    Digests the *text*, so two files that differ only in BOM or line endings
    compare equal; use :func:`file_digest` when what matters is the bytes on
    disk, and never compare one against the other.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# atomic writes
# --------------------------------------------------------------------------
def atomic_write(path: str | os.PathLike, data: str | bytes,
                 encoding: Encoding | None = None, *,
                 expect_digest: str | None = None,
                 mode: int | None = None) -> str:
    """Write ``data`` so that the destination is never seen half-written.

    The temp file is created in the destination's own directory, because
    :func:`os.replace` is only atomic within a filesystem; it is flushed and
    fsynced before the rename so a crash cannot leave a correctly named file
    full of zeros, and the directory is fsynced afterwards on POSIX so the
    rename itself survives a power cut.

    ``expect_digest`` is a value previously obtained from :func:`file_digest`
    (``""`` meaning "the file must not exist").  When it is given and the
    destination no longer matches, :class:`ExternalChange` is raised *before*
    anything is written, so a save cannot silently discard an edit made in
    another program - or land in a file that was swapped for a symlink after
    the caller validated it.

    ``mode`` sets the destination's permission bits; by default an existing
    file's mode is preserved (``os.replace`` would otherwise donate the temp
    file's) and a new file is created ``0o600``, since exported job files can
    carry unpublished geometry.  Returns the canonical destination path.
    """
    target = canonical_path(path)
    directory = os.path.dirname(target) or os.curdir
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"{directory}: destination directory does not "
                                "exist")

    if isinstance(data, str):
        enc = encoding or DEFAULT_ENCODING
        raw = enc.encode(data)
    elif isinstance(data, (bytes, bytearray, memoryview)):
        if encoding is not None:
            raise ValueError("encoding= is meaningless for bytes; encode the "
                             "text yourself or pass the str")
        raw = bytes(data)
    else:
        raise TypeError(f"data must be str or bytes, not "
                        f"{type(data).__name__}")

    if expect_digest is not None:
        current = file_digest(target)
        if current != expect_digest:
            raise ExternalChange(
                f"{target} changed on disk: expected sha256 "
                f"{expect_digest or '(absent)'}, found "
                f"{current or '(absent)'}")

    if mode is None:
        try:
            mode = stat.S_IMODE(os.stat(target).st_mode)
        except OSError:
            mode = 0o600

    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(target) + ".",
                               suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # Durability of the rename itself. Not available on Windows, where
    # opening a directory fails; NTFS journals the metadata change anyway.
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        pass
    else:
        try:
            os.fsync(dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)
    return target


# --------------------------------------------------------------------------
# archives
# --------------------------------------------------------------------------
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)])

#: Characters Windows forbids in a file name. ':' also catches the
#: drive-relative form ``C:evil`` that ntpath.isabs() considers relative.
_WINDOWS_FORBIDDEN = set('<>:"|?*')

MAX_MEMBER_NAME = 1024


def _member_parts(name: str) -> tuple[str, ...]:
    """Split an archive member name into safe path components.

    Every rejection here is a name that would escape the destination, name a
    device, or mean two different things on two operating systems.  The rules
    are the union of POSIX and Windows semantics so that an archive rejected
    on one platform is rejected on both - a package that extracts differently
    depending on where the scientist opened it is a bug even when it is not an
    attack.
    """
    if not name:
        raise UnsafeArchive("archive member with an empty name")
    if len(name) > MAX_MEMBER_NAME:
        raise UnsafeArchive(f"archive member name is {len(name)} characters "
                            f"long (limit {MAX_MEMBER_NAME})")
    if "\x00" in name:
        raise UnsafeArchive(f"archive member {name!r} contains a NUL byte")
    if "\\" in name:
        raise UnsafeArchive(
            f"archive member {name!r} contains a backslash; ZIP names use "
            "'/' and a backslash is a directory separator on Windows")
    if posixpath.isabs(name) or ntpath.isabs(name):
        raise UnsafeArchive(f"archive member {name!r} is an absolute path")
    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafeArchive(f"archive member {name!r} traverses out of "
                                "the destination directory")
        if part.rstrip(" .") != part:
            raise UnsafeArchive(
                f"archive member {name!r} has a component ending in a space "
                "or a dot; Windows strips those, so two members could collide")
        bad = _WINDOWS_FORBIDDEN.intersection(part)
        if bad:
            raise UnsafeArchive(
                f"archive member {name!r} contains {''.join(sorted(bad))!r}, "
                "which is not a legal file name character on Windows")
        if any(ord(ch) < 32 for ch in part):
            raise UnsafeArchive(f"archive member {name!r} contains a control "
                                "character")
        if part.split(".")[0].upper() in _WINDOWS_RESERVED:
            raise UnsafeArchive(f"archive member {name!r} names the reserved "
                                "Windows device "
                                f"{part.split('.')[0].upper()}")
        parts.append(part)
    if not parts:
        raise UnsafeArchive(f"archive member {name!r} names no file")
    return tuple(parts)


def _member_mode(info: zipfile.ZipInfo) -> int:
    """The Unix mode a member declares, or 0 when it declares none."""
    if info.create_system in (3, 19):          # 3 = Unix, 19 = macOS
        return (info.external_attr >> 16) & 0xFFFF
    return 0


def _reject_member_type(name: str, mode: int) -> None:
    """Refuse anything that is not a regular file or a directory.

    A member with permission bits but no file-type bits is the normal output
    of :meth:`zipfile.ZipFile.writestr`, which stores ``0o600 << 16``; that is
    a regular file, not an unknown type.
    """
    if not stat.S_IFMT(mode) or stat.S_ISREG(mode) or stat.S_ISDIR(mode):
        return
    kind = ("symbolic link" if stat.S_ISLNK(mode) else
            "FIFO" if stat.S_ISFIFO(mode) else
            "character device" if stat.S_ISCHR(mode) else
            "block device" if stat.S_ISBLK(mode) else
            "socket" if stat.S_ISSOCK(mode) else
            f"file of type 0o{stat.S_IFMT(mode):o}")
    raise UnsafeArchive(f"archive member {name!r} is a {kind}; only regular "
                        "files and directories are extracted")


def _contains(parent_real: str, child: str) -> bool:
    p = os.path.normcase(parent_real).rstrip(os.sep)
    c = os.path.normcase(child)
    return c == p or c.startswith(p + os.sep)


def safe_extract_zip(zip_path: str | os.PathLike, dest_dir: str | os.PathLike,
                     limits: Limits = DEFAULT_LIMITS) -> list[str]:
    """Extract a ZIP into ``dest_dir``, refusing anything hostile.

    Members are never handed to :meth:`zipfile.ZipFile.extract`, which trusts
    the name in the archive.  Each name is re-validated by
    :func:`_member_parts`, joined onto the destination by this process, and
    the *resolved* parent directory is checked to be inside the destination -
    which catches the case where the archive first creates (or the destination
    already contains) a symlinked subdirectory pointing elsewhere.  Files are
    opened ``O_CREAT|O_EXCL|O_NOFOLLOW``, so an existing name is never
    overwritten and a planted link is never followed, and the payload is
    streamed with the total budget decremented as bytes land.

    Refusals: absolute or traversing names, backslashes and drive letters,
    Windows-reserved and Windows-illegal names, duplicate names (case
    insensitively, since the destination may be case-insensitive), symlink and
    device members, encrypted members, more than ``max_archive_members``
    entries, a member larger than ``max_file_bytes``, a total larger than
    ``max_archive_bytes``, and any member whose uncompressed/compressed ratio
    exceeds ``max_archive_ratio``.

    Extracted files are created ``0o600`` and the mode stored in the archive
    is ignored: a package must not be able to hand a scientist an executable
    or a world-writable file.

    Returns the list of extracted *file* paths in archive order; directories
    are created as needed but not listed.  A corrupt or forged archive raises
    :class:`zipfile.BadZipFile` - the CRC check is what catches a header that
    understates a member's size.  On failure a partially written member is
    removed but members already extracted are left in place, so callers should
    extract into a directory they can discard.
    """
    archive = canonical_path(zip_path, must_exist=True, must_be_file=True)
    enforce_limit(f"{archive}: archive file size", os.path.getsize(archive),
                  limits.max_archive_bytes)

    dest = canonical_path(dest_dir)
    os.makedirs(dest, exist_ok=True)
    if not os.path.isdir(dest):
        raise ValueError(f"{dest}: destination is not a directory")
    dest_real = os.path.realpath(dest)

    extracted: list[str] = []
    seen: set = set()
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        enforce_limit(f"{archive}: number of archive members", len(infos),
                      limits.max_archive_members)
        for info in infos:
            name = info.filename
            if info.flag_bits & 0x1:
                raise UnsafeArchive(f"archive member {name!r} is encrypted; "
                                    "this reader does not decrypt archives")
            parts = _member_parts(name)
            key = tuple(p.lower() for p in parts)
            if key in seen:
                raise UnsafeArchive(f"archive contains {name!r} twice (names "
                                    "are compared case-insensitively)")
            seen.add(key)

            mode = _member_mode(info)
            _reject_member_type(name, mode)
            target = os.path.join(dest, *parts)

            if info.is_dir() or stat.S_ISDIR(mode):
                os.makedirs(target, exist_ok=True)
                if not _contains(dest_real, os.path.realpath(target)):
                    raise UnsafeArchive(
                        f"archive member {name!r} resolves outside the "
                        f"destination directory")
                continue

            enforce_limit(f"archive member {name!r} uncompressed size",
                          info.file_size, limits.max_file_bytes)
            enforce_limit(f"{archive}: total uncompressed size",
                          total + info.file_size, limits.max_archive_bytes)
            # Below a kilobyte the ratio is meaningless (a 1-byte file stored
            # in a 2-byte deflate block "expands"), and no bomb is built out
            # of members that small.
            if info.file_size >= 1024:
                ratio = (info.file_size / info.compress_size
                         if info.compress_size else float("inf"))
                enforce_limit(f"archive member {name!r} compression ratio",
                              ratio, limits.max_archive_ratio)

            parent = os.path.dirname(target)
            os.makedirs(parent, exist_ok=True)
            if not _contains(dest_real, os.path.realpath(parent)):
                raise UnsafeArchive(
                    f"archive member {name!r} resolves outside the "
                    f"destination directory (its parent is a link)")
            total += _write_member(zf, info, target,
                                   limits.max_archive_bytes - total)
            extracted.append(target)
    return extracted


def _write_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: str,
                  budget: int) -> int:
    """Stream one member to ``target``; return the number of bytes written."""
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise UnsafeArchive(
            f"archive member {info.filename!r} would overwrite the existing "
            f"{target}") from exc
    except OSError as exc:                      # ELOOP: the name is a symlink
        raise UnsafeArchive(f"archive member {info.filename!r} cannot be "
                            f"written to {target}: {exc}") from exc
    written = 0
    try:
        with os.fdopen(fd, "wb") as out, zf.open(info, "r") as src:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                # CPython's ZipExtFile already truncates a member at its
                # declared size and then fails the CRC, so a lying header
                # cannot get past this; the checks keep the guarantee if that
                # implementation detail ever changes.
                enforce_limit(f"archive member {info.filename!r} actual size",
                              written, info.file_size)
                enforce_limit(f"archive member {info.filename!r} against the "
                              "remaining archive budget", written, budget)
                out.write(chunk)
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    return written


# --------------------------------------------------------------------------
# child processes
# --------------------------------------------------------------------------
#: Variables a child needs to find its files and format its numbers. Anything
#: that can make an interpreter or a linker load someone else's code
#: (LD_PRELOAD, LD_LIBRARY_PATH, PYTHONPATH, PYTHONSTARTUP, BASH_ENV, IFS ...)
#: is absent by construction: this is an allowlist, so a variable nobody
#: thought of is dropped rather than forwarded.
_KEEP_POSIX = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "TMPDIR", "SCHRODINGER",
})
_KEEP_WINDOWS = frozenset({
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
    "LOCALAPPDATA", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "SCHRODINGER",
})


def _clean_path(value: str) -> str:
    """Drop empty and relative ``PATH`` entries.

    An empty entry means the current directory, so a job launched from a
    directory an attacker can write to would run their ``multisim`` instead of
    Schrodinger's.
    """
    kept = [e for e in value.split(os.pathsep) if e and e != os.curdir
            and os.path.isabs(e)]
    return os.pathsep.join(kept)


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a scrubbed environment for a child process.

    Only the variables in the platform allowlist are inherited, ``PATH`` is
    stripped of relative entries, and ``PYTHONNOUSERSITE`` is set so a child
    Python (``$SCHRODINGER/run python3``) cannot pick up packages from the
    user's site directory.  ``extra`` is applied last and is the only way to
    add anything; its keys and values must be strings without NUL bytes, and
    a key may not be empty or contain ``=``.

    :data:`os.environ` is never modified.  On Windows keys are upper-cased
    because the environment block is case-insensitive and a dict is not - a
    branch not exercised on Linux.
    """
    windows = os.name == "nt"
    keep = _KEEP_WINDOWS if windows else _KEEP_POSIX
    env: dict = {}
    for key in keep:
        value = os.environ.get(key)
        if value is not None and "\x00" not in value:
            env[key] = value
    env["PATH"] = _clean_path(env.get("PATH") or os.defpath)
    env["PYTHONNOUSERSITE"] = "1"

    for key, value in (extra or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"environment entry {key!r} must be str -> str")
        if not key or "=" in key or "\x00" in key:
            raise ValueError(f"illegal environment variable name {key!r}")
        if "\x00" in value:
            raise ValueError(f"value of {key} contains a NUL byte")
        env[key.upper() if windows else key] = value
    return env


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------
def _selftest() -> int:
    """Exercise every rejection path and every round trip; return failures.

    Kept in the module because these are the checks the threat model points
    at: ``python -m funnelforge.core.safety`` must pass on any machine the
    application is installed on, not only where the test suite is present.
    """
    import shutil

    passed = failed = 0

    def ok(label: str, detail: str = "") -> None:
        nonlocal passed
        passed += 1
        print(f"  ok   {label}" + (f" -> {detail}" if detail else ""))

    def bad(label: str, detail: str) -> None:
        nonlocal failed
        failed += 1
        print(f"  FAIL {label} -> {detail}")

    def expect(label: str, exc_types, fn) -> None:
        try:
            fn()
        except exc_types as exc:
            ok(label, f"{type(exc).__name__}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            bad(label, f"wrong exception {type(exc).__name__}: {exc}")
        else:
            bad(label, "no exception raised")

    def check(label: str, condition: bool, detail: str = "",
              quiet: bool = False) -> None:
        nonlocal passed
        if condition and quiet:
            passed += 1                    # counted, printed only on failure
            return
        (ok if condition else bad)(label, detail)

    saved_env = dict(os.environ)
    root = tempfile.mkdtemp(prefix="ff_safety_selftest_")
    try:
        print("[encodings] every codec x line ending x final newline, "
              "byte for byte")
        seps = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}
        bodies = [
            ("utf-8", b"", "utf-8", ["core = 1.0;", "k = 2.5;", "# olcum"]),
            ("utf-8-sig", codecs.BOM_UTF8, "utf-8",
             ["core = 1.0;", "k = 2.5;", "# yuzey"]),
            ("utf-16-le", codecs.BOM_UTF16_LE, "utf-16-le",
             ["core = 1.0;", "k = 2.5;", "# note"]),
            ("utf-16-be", codecs.BOM_UTF16_BE, "utf-16-be",
             ["core = 1.0;", "k = 2.5;", "# note"]),
            ("utf-32-le", codecs.BOM_UTF32_LE, "utf-32-le",
             ["core = 1.0;", "k = 2.5;", "# note"]),
            ("utf-32-be", codecs.BOM_UTF32_BE, "utf-32-be",
             ["core = 1.0;", "k = 2.5;", "# note"]),
            # bytes that are not valid UTF-8, so the fallback has to catch it
            ("latin-1", b"", "latin-1",
             ["core = 1.0;", "# \xf6l\xe7\xfcm", "k = 2.5;"]),
        ]
        for name, bom, codec, lines in bodies:
            combos = 0
            for kind in NEWLINE_KINDS:
                for final in (True, False):
                    if kind == "mixed":
                        text, tail = lines[0] + "\n" + lines[1] + "\r\n" \
                            + lines[2], "\n"
                    else:
                        text, tail = seps[kind].join(lines), seps[kind]
                    raw = bom + (text + tail if final else text).encode(codec)
                    enc = sniff_encoding(raw)
                    good = (enc.encode(enc.decode(raw)) == raw
                            and (enc.name, enc.bom, enc.newline,
                                 enc.final_newline)
                            == (name, bool(bom), kind, final))
                    check(f"{name}/{kind}/final={final}", good,
                          f"sniffed as {enc.describe()}", quiet=True)
                    combos += 1
            check(f"{name:<10} {combos} combinations round-trip", True,
                  "identical bytes")

        for label, raw in [("empty file", b""),
                           ("no newline at all", b"k = 1.0;"),
                           ("three kinds at once", b"a;\nb;\r\nc;\rd;")]:
            enc = sniff_encoding(raw)
            check(f"{label:<28} [{enc.describe()}]",
                  enc.encode(enc.decode(raw)) == raw, "identical bytes")

        enc = sniff_encoding(b"a;\r\nb;\r\n")
        check("CRLF file decodes to LF only",
              enc.decode(b"a;\r\nb;\r\n") == "a;\nb;\n", "no CR in the text")
        check("CRLF encode re-applies the convention",
              enc.encode("a;\nb;\nc;\n") == b"a;\r\nb;\r\nc;\r\n",
              "edited text keeps CRLF")
        check("pasted CRLF does not corrupt an LF file",
              DEFAULT_ENCODING.encode("a;\r\nb;\n") == b"a;\nb;\n",
              "normalised to LF")
        check("mixed file is passed through untouched",
              sniff_encoding(b"a;\nb;\r\n").decode(b"a;\nb;\r\n")
              == "a;\nb;\r\n", "reported, not rewritten")
        expect("Encoding rejects an inconsistent BOM flag", ValueError,
               lambda: Encoding("utf-8", True, "lf", True))
        expect("Encoding rejects an unknown newline kind", ValueError,
               lambda: Encoding("utf-8", False, "nel", True))
        expect("Encoding refuses bytes without the BOM it expects", ValueError,
               lambda: Encoding("utf-8-sig", True, "lf", True).decode(b"x"))

        print("[read_text_file]")
        pot = os.path.join(root, "sample.pot")
        with open(pot, "wb") as fh:
            fh.write(codecs.BOM_UTF8 + b"core = 1.0;\r\n")
        text, enc = read_text_file(pot)
        check("reads through the BOM", text == "core = 1.0;\n",
              f"{enc.describe()}")
        expect("refuses a file over max_file_bytes", LimitExceeded,
               lambda: read_text_file(pot, dataclasses.replace(
                   DEFAULT_LIMITS, max_file_bytes=4)))
        expect("refuses a directory", ValueError,
               lambda: read_text_file(root))
        expect("refuses a missing file", FileNotFoundError,
               lambda: read_text_file(os.path.join(root, "nope.pot")))

        print("[canonical_path]")
        check("makes a relative path absolute",
              os.path.isabs(canonical_path("sample.pot")),
              canonical_path("sample.pot"))
        expect("rejects an empty path", ValueError, lambda: canonical_path(""))
        expect("rejects a NUL byte", ValueError,
               lambda: canonical_path("a\x00b"))
        expect("rejects a non-path", ValueError, lambda: canonical_path(3))
        link = os.path.join(root, "link.pot")
        os.symlink(pot, link)
        check("keeps a symlink when links are allowed",
              canonical_path(link) == link, link)
        expect("rejects a symlink when links are not allowed", ValueError,
               lambda: canonical_path(link, allow_symlink=False))
        linkdir = os.path.join(root, "linkdir")
        os.symlink(root, linkdir)
        expect("rejects a symlink anywhere in the chain", ValueError,
               lambda: canonical_path(os.path.join(linkdir, "sample.pot"),
                                      allow_symlink=False))

        print("[digests and atomic_write]")
        out = os.path.join(root, "out.pot")
        check("file_digest of an absent file is empty",
              file_digest(out) == "", '""')
        atomic_write(out, "core = 1.0;\n", expect_digest="")
        d0 = file_digest(out)
        check("wrote a new file", open(out).read() == "core = 1.0;\n", d0[:16])
        check("text_digest matches the written text",
              text_digest("core = 1.0;\n") == d0,
              "utf-8 LF text and file agree")
        expect("refuses to create a file that already exists", ExternalChange,
               lambda: atomic_write(out, "x", expect_digest=""))
        atomic_write(out, "core = 2.0;\n", expect_digest=d0)
        d1 = file_digest(out)
        check("wrote with the correct expected digest",
              open(out).read() == "core = 2.0;\n", d1[:16])
        with open(out, "w") as fh:                     # somebody else edits it
            fh.write("core = 99.0;\n")
        expect("detects an external change before writing", ExternalChange,
               lambda: atomic_write(out, "core = 3.0;\n", expect_digest=d1))
        check("the external edit survived the refusal",
              open(out).read() == "core = 99.0;\n", "nothing was written")
        check("no temp files were left behind",
              not [n for n in os.listdir(root) if n.endswith(".tmp")],
              "directory is clean")

        crlf = Encoding("utf-8", False, "crlf", True)
        atomic_write(os.path.join(root, "crlf.pot"), "a;\nb;\n", crlf)
        with open(os.path.join(root, "crlf.pot"), "rb") as fh:
            check("atomic_write honours the file's line endings",
                  fh.read() == b"a;\r\nb;\r\n", "CRLF preserved")
        atomic_write(os.path.join(root, "raw.bin"), b"\x00\x01\x02")
        check("atomic_write takes bytes",
              open(os.path.join(root, "raw.bin"), "rb").read()
              == b"\x00\x01\x02", "3 bytes")
        expect("rejects an encoding with bytes", ValueError,
               lambda: atomic_write(os.path.join(root, "raw.bin"), b"x",
                                    DEFAULT_ENCODING))
        expect("rejects a non-existent destination directory",
               FileNotFoundError,
               lambda: atomic_write(os.path.join(root, "no", "such", "f"),
                                    "x"))
        os.chmod(out, 0o640)
        atomic_write(out, "core = 4.0;\n")
        check("preserves the destination's mode",
              stat.S_IMODE(os.stat(out).st_mode) == 0o640, "0o640")
        check("creates new files private",
              stat.S_IMODE(os.stat(os.path.join(root, "raw.bin")).st_mode)
              == 0o600, "0o600")

        outside = os.path.join(root, "outside.txt")
        with open(outside, "w") as fh:
            fh.write("do not touch\n")
        swapped = os.path.join(root, "swapped.pot")
        os.symlink(outside, swapped)
        atomic_write(swapped, "mine\n")
        check("a symlink at the destination is replaced, not followed",
              open(outside).read() == "do not touch\n"
              and not os.path.islink(swapped)
              and open(swapped).read() == "mine\n",
              "the link target is untouched")

        print("[safe_extract_zip] happy path")
        good = os.path.join(root, "good.zip")
        with zipfile.ZipFile(good, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("job/job.pot", "core = 1.0;\n")
            zf.writestr("job/sub/job.cfg", "time = 100.0\n")
            zf.writestr("job/empty/", "")
        dest = os.path.join(root, "x_good")
        got = safe_extract_zip(good, dest)
        check("extracts a legitimate archive", len(got) == 2, str(
            [os.path.relpath(p, dest) for p in got]))
        check("contents survive the round trip",
              open(os.path.join(dest, "job", "job.pot")).read()
              == "core = 1.0;\n", "job/job.pot")
        check("explicit directory members are created",
              os.path.isdir(os.path.join(dest, "job", "empty")), "job/empty/")

        print("[safe_extract_zip] rejections")

        def make(name: str, members) -> str:
            path = os.path.join(root, name)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                for member in members:
                    if isinstance(member, tuple):
                        zf.writestr(member[0], member[1])
                    else:
                        zf.writestr(member, "payload\n")
            return path

        def unix_member(name: str, mode: int, data: str = "") -> zipfile.ZipInfo:
            zi = zipfile.ZipInfo(name)
            zi.create_system = 3
            zi.external_attr = mode << 16
            return zi

        def run(path: str, sub: str, lim: Limits = DEFAULT_LIMITS):
            return lambda: safe_extract_zip(path, os.path.join(root, sub), lim)

        expect("absolute member", UnsafeArchive,
               run(make("abs.zip", ["/etc/passwd"]), "x_abs"))
        expect("'..' traversal", UnsafeArchive,
               run(make("trav.zip", ["../escaped.txt"]), "x_trav"))
        expect("nested '..' traversal", UnsafeArchive,
               run(make("trav2.zip", ["job/../../escaped.txt"]), "x_trav2"))
        expect("backslash separator", UnsafeArchive,
               run(make("back.zip", ["..\\escaped.txt"]), "x_back"))
        expect("windows drive letter", UnsafeArchive,
               run(make("drive.zip", ["C:evil.txt"]), "x_drive"))
        expect("windows UNC path", UnsafeArchive,
               run(make("unc.zip", ["//server/share/evil.txt"]), "x_unc"))
        expect("reserved windows device name", UnsafeArchive,
               run(make("dev.zip", ["job/CON.txt"]), "x_dev"))
        expect("component ending in a dot", UnsafeArchive,
               run(make("dot.zip", ["job./evil.txt"]), "x_dot"))
        expect("illegal windows character", UnsafeArchive,
               run(make("colon.zip", ['job/we"ird.txt']), "x_colon"))
        expect("control character in the name", UnsafeArchive,
               run(make("ctrl.zip", ["job/a\x01b.txt"]), "x_ctrl"))
        expect("empty member name", UnsafeArchive,
               run(make("empty.zip", ["./"]), "x_empty"))
        expect("over-long member name", UnsafeArchive,
               run(make("long.zip", ["a" * 1100]), "x_long"))
        expect("duplicate member names", UnsafeArchive,
               run(make("dup.zip", ["job/a.txt", "job/A.txt"]), "x_dup"))

        sym = os.path.join(root, "sym.zip")
        with zipfile.ZipFile(sym, "w") as zf:
            zf.writestr(unix_member("job/link", 0o120777), "/etc/passwd")
        expect("symlink member", UnsafeArchive, run(sym, "x_sym"))

        fifo = os.path.join(root, "fifo.zip")
        with zipfile.ZipFile(fifo, "w") as zf:
            zf.writestr(unix_member("job/pipe", 0o010600), "")
        expect("FIFO member", UnsafeArchive, run(fifo, "x_fifo"))

        chardev = os.path.join(root, "chardev.zip")
        with zipfile.ZipFile(chardev, "w") as zf:
            zf.writestr(unix_member("job/null", 0o020666), "")
        expect("character-device member", UnsafeArchive,
               run(chardev, "x_chardev"))

        # zipfile clears the flag word when it writes, so the encryption bit
        # has to be set afterwards, in both the local and the central header.
        enc_zip = os.path.join(root, "encrypted.zip")
        with zipfile.ZipFile(enc_zip, "w") as zf:
            zf.writestr("job/secret.pot", "core = 1.0;\n")
        buf = bytearray(open(enc_zip, "rb").read())
        buf[buf.find(b"PK\x03\x04") + 6] |= 0x1
        buf[buf.find(b"PK\x01\x02") + 8] |= 0x1
        with open(enc_zip, "wb") as fh:
            fh.write(buf)
        expect("encrypted member", UnsafeArchive, run(enc_zip, "x_enc"))

        expect("too many members", LimitExceeded,
               run(make("many.zip", [f"job/f{i}.txt" for i in range(6)]),
                   "x_many", dataclasses.replace(DEFAULT_LIMITS,
                                                 max_archive_members=5)))
        expect("member over max_file_bytes", LimitExceeded,
               run(make("big.zip", [("job/big.txt", "x" * 5000)]), "x_big",
                   dataclasses.replace(DEFAULT_LIMITS, max_file_bytes=1000)))
        expect("total over max_archive_bytes", LimitExceeded,
               run(make("total.zip", [("job/a.txt", "x" * 3000),
                                      ("job/b.txt", "y" * 3000)]), "x_total",
                   dataclasses.replace(DEFAULT_LIMITS,
                                       max_archive_bytes=4000)))
        bomb = make("bomb.zip", [("job/bomb.bin", "\0" * (4 << 20))])
        check("the bomb really is small on disk",
              os.path.getsize(bomb) < 64 * 1024,
              f"{os.path.getsize(bomb)} bytes holding 4 MiB")
        expect("compression-ratio bomb", LimitExceeded, run(bomb, "x_bomb"))
        expect("archive file itself over max_archive_bytes", LimitExceeded,
               run(good, "x_smallcap", dataclasses.replace(
                   DEFAULT_LIMITS, max_archive_bytes=16)))

        planted = os.path.join(root, "x_planted")
        os.makedirs(planted)
        os.symlink(outside, os.path.join(planted, "job.pot"))
        expect("member landing on a planted symlink", UnsafeArchive,
               lambda: safe_extract_zip(make("plain.zip", ["job.pot"]),
                                        planted))
        check("the planted link's target is untouched",
              open(outside).read() == "do not touch\n", "not written through")

        escape_dir = os.path.join(root, "x_escape")
        os.makedirs(escape_dir)
        os.symlink(root, os.path.join(escape_dir, "job"))
        expect("member below a symlinked subdirectory", UnsafeArchive,
               lambda: safe_extract_zip(make("sub.zip", ["job/x.txt"]),
                                        escape_dir))

        forged = os.path.join(root, "forged.zip")
        with zipfile.ZipFile(forged, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("job/lie.txt", "z" * 4000)
        raw = open(forged, "rb").read()
        real = (4000).to_bytes(4, "little")
        check("forged archive: found the declared size",
              raw.count(real) >= 2, f"{raw.count(real)} occurrences")
        with open(forged, "wb") as fh:
            fh.write(raw.replace(real, (16).to_bytes(4, "little")))
        expect("header understating a member's size", zipfile.BadZipFile,
               run(forged, "x_forged"))

        expect("not a zip at all", zipfile.BadZipFile,
               run(pot, "x_notzip"))

        print("[minimal_env]")
        os.environ["LD_PRELOAD"] = "/tmp/evil.so"
        os.environ["PYTHONPATH"] = "/tmp/evil"
        os.environ["BASH_ENV"] = "/tmp/evil.sh"
        os.environ["PATH"] = os.pathsep.join(
            ["", ".", "relative/bin", "/usr/bin", "/bin"])
        os.environ["SCHRODINGER"] = "/opt/schrodinger2025-3"
        env = minimal_env()
        check("drops LD_PRELOAD", "LD_PRELOAD" not in env, "absent")
        check("drops PYTHONPATH", "PYTHONPATH" not in env, "absent")
        check("drops BASH_ENV", "BASH_ENV" not in env, "absent")
        check("keeps SCHRODINGER",
              env.get("SCHRODINGER") == "/opt/schrodinger2025-3",
              env.get("SCHRODINGER", ""))
        check("cleans PATH", env["PATH"] == "/usr/bin" + os.pathsep + "/bin",
              env["PATH"])
        check("sets PYTHONNOUSERSITE", env.get("PYTHONNOUSERSITE") == "1", "1")
        check("os.environ is untouched",
              os.environ.get("LD_PRELOAD") == "/tmp/evil.so", "still set")
        check("extra is applied",
              minimal_env({"JOBNAME": "run1"}).get("JOBNAME") == "run1",
              "JOBNAME=run1")
        expect("rejects a non-string value", ValueError,
               lambda: minimal_env({"N": 1}))
        expect("rejects '=' in a name", ValueError,
               lambda: minimal_env({"A=B": "c"}))
        expect("rejects a NUL in a value", ValueError,
               lambda: minimal_env({"A": "b\x00c"}))

        print("[limits]")
        check("defaults are sane",
              DEFAULT_LIMITS.max_file_bytes == 32 * 1024 * 1024,
              f"max_file_bytes={DEFAULT_LIMITS.max_file_bytes}")
        expect("rejects a zero ceiling", ValueError,
               lambda: Limits(max_tokens=0))
        expect("rejects a negative ceiling", ValueError,
               lambda: Limits(max_ast_depth=-1))
        expect("rejects a ratio below 1", ValueError,
               lambda: Limits(max_archive_ratio=0.5))
        expect("enforce_limit refuses over the cap", LimitExceeded,
               lambda: enforce_limit("nesting depth", 300,
                                     DEFAULT_LIMITS.max_ast_depth))
        enforce_limit("nesting depth", 256, DEFAULT_LIMITS.max_ast_depth)
        ok("enforce_limit allows exactly the cap", "256 <= 256")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        os.environ.clear()                 # the env test plants hostile values
        os.environ.update(saved_env)

    print(f"\n{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
