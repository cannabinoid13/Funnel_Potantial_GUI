"""The other three files of a Desmond funnel-metadynamics job.

``CfgFile``  - the Desmond backend configuration (production stage settings).
``MsjFile``  - the Multisim stage chain (relaxation -> equilibration -> MetaD).
``ShFile``   - the multisim launch script.
``JobBundle`` - all four files plus the .cms, kept consistent and exported
               together as a ready-to-run set.

Edits are surgical: values are replaced on their own text span so comments and
layout are preserved, which matters because these files are human-reviewed.
"""

from __future__ import annotations

import os
import re
import shutil
import atexit
import json
import shlex
import tempfile
from dataclasses import dataclass, field, replace

from .safety import read_text_file, safe_extract_zip, file_digest, DEFAULT_LIMITS

from .blocktext import BlockText, unquote
from .funnel import Issue, FunnelSpec

# --------------------------------------------------------------------------
# .cfg
# --------------------------------------------------------------------------
CFG_NUMERIC_KEYS = [
    ("time", "Production length (ps)"),
    ("cutoff_radius", "Non-bonded cutoff (A)"),
    ("trajectory.interval", "Trajectory interval (ps)"),
    ("eneseq.interval", "Energy log interval (ps)"),
    ("checkpt.interval", "Checkpoint interval (ps)"),
    ("maeff_output.interval", "Structure output interval (ps)"),
    ("ensemble.thermostat.tau", "Thermostat tau (ps)"),
    ("ensemble.barostat.tau", "Barostat tau (ps)"),
    ("cpu", "CPU / GPU count"),
    ("randomize_velocity.seed", "Velocity seed"),
]


class CfgFile:
    def __init__(self, text: str, path: str = ""):
        self.path = path
        self.doc = BlockText(text)

    # -- basics
    @property
    def text(self) -> str:
        return self.doc.text

    @classmethod
    def load(cls, path: str) -> "CfgFile":
        with open(path) as fh:
            return cls(fh.read(), path)

    def value(self, path: str, default=None):
        v = self.doc.value(path, None)
        return default if v is None else v

    def number(self, path: str, default=None):
        return self.doc.float_value(path, default)

    def set_number(self, path: str, value: float, integer: bool = False) -> bool:
        txt = str(int(round(value))) if integer else _fmt_ps(value)
        return self.doc.set_value(path, txt)

    def set_raw(self, path: str, text: str) -> bool:
        return self.doc.set_value(path, text)

    # -- temperature: `temperature = [ [310.0 0] ]`
    def temperature(self) -> float | None:
        node = self.doc.get("temperature")
        if node is None:
            return None
        raw = self.doc.raw(node)
        m = re.search(r"([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)", raw)
        return float(m.group(1)) if m else None

    def set_temperature(self, value: float) -> bool:
        node = self.doc.get("temperature")
        if node is None:
            return False
        raw = self.doc.raw(node)
        new = re.sub(r"([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)",
                     _fmt_ps(value), raw, count=1)
        self.doc.set_node(node, new)
        return True

    def timestep(self) -> list[float]:
        raw = self.value("timestep", "")
        return [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?",
                                            str(raw))]

    def set_timestep(self, inner: float, rc: float | None = None,
                     far: float | None = None) -> bool:
        rc = inner if rc is None else rc
        far = inner * 3 if far is None else far
        return self.doc.set_value(
            "timestep", f"[{_fmt_ps(inner)} {_fmt_ps(rc)} {_fmt_ps(far)}]")

    # -- validation
    def validate(self, spec: FunnelSpec | None = None,
                 box_min: float | None = None) -> list[Issue]:
        out: list[Issue] = []
        t = self.number("time")
        if t is None or t <= 0:
            out.append(Issue("error", "cfg", "`time` is missing or not positive",
                             "This is the production length in ps."))
        cut = self.number("cutoff_radius")
        if cut and box_min and box_min < 2 * cut:
            out.append(Issue("error", "cfg",
                             f"Box edge {box_min:.1f} A is shorter than twice "
                             f"the {cut:.1f} A cutoff",
                             "Desmond requires L >= 2 * cutoff_radius."))
        mf = self.value("meta_file")
        if mf is not None and str(mf).strip() not in ("?", ""):
            out.append(Issue("warning", "cfg",
                             f"`meta_file` is hard-coded to {mf!r} in the .cfg",
                             "Multisim overwrites it from the .msj stage; leave "
                             "it as `?` to avoid a stale path."))
        rv = self.value("randomize_velocity.interval")
        if rv is not None and str(rv).strip() != "inf":
            out.append(Issue("warning", "cfg",
                             f"Velocities are re-randomised every {rv} ps",
                             "During metadynamics production this destroys the "
                             "dynamics; use `inf`."))
        ens = self.value("ensemble.class")
        if ens not in ("NPT", "NVT", None):
            out.append(Issue("warning", "cfg", f"Unusual ensemble class {ens!r}", ""))
        ts = self.timestep()
        if ts and ts[0] > 0.0025:
            out.append(Issue("warning", "cfg",
                             f"Inner timestep {ts[0] * 1000:.1f} fs is large",
                             "2 fs is the usual maximum with constrained "
                             "hydrogens."))
        if t:
            ti = self.number("trajectory.interval")
            if ti and ti > 0:
                nfr = t / ti
                if nfr > 50000:
                    out.append(Issue("warning", "cfg",
                                     f"{nfr:.0f} trajectory frames requested",
                                     "Consider a longer trajectory.interval."))
                else:
                    out.append(Issue("info", "cfg",
                                     f"{nfr:.0f} trajectory frames "
                                     f"({ti:g} ps apart)", ""))
            if spec is not None and spec.cv_interval > 0:
                out.append(Issue("info", "cfg",
                                 f"{t / spec.cv_interval:.0f} CV records in "
                                 f"{os.path.basename(spec.cvseq_name)}", ""))
        cp = self.value("checkpt.write_last_step")
        if cp is not None and str(cp).strip().lower() not in ("true", "yes"):
            out.append(Issue("warning", "cfg",
                             "`checkpt.write_last_step` is off in the .cfg",
                             "Without a final checkpoint the run cannot be "
                             "extended."))
        return out


def _fmt_ps(v: float) -> str:
    f = float(v)
    if f == int(f) and abs(f) < 1e12:
        return f"{int(f)}.0"
    return repr(f)


# --------------------------------------------------------------------------
# .msj
# --------------------------------------------------------------------------
@dataclass
class Stage:
    index: int
    name: str
    node: object
    title: str = ""
    time_ps: float | None = None
    is_production: bool = False
    meta_mode: str = ""
    meta_file: str = ""
    cfg_file: str = ""


class MsjFile:
    def __init__(self, text: str, path: str = ""):
        self.path = path
        self.doc = BlockText(text)
        self.stages: list[Stage] = []
        self._scan()

    @property
    def text(self) -> str:
        return self.doc.text

    @classmethod
    def load(cls, path: str) -> "MsjFile":
        with open(path) as fh:
            return cls(fh.read(), path)

    def _scan(self) -> None:
        self.stages = []
        for i, node in enumerate(self.doc.root.children):
            if node.kind != "block":
                continue
            st = Stage(index=i, name=node.key, node=node)
            t = node.child("title")
            st.title = unquote(self.doc.raw(t)) if t else ""
            tm = node.child("time")
            if tm is not None:
                try:
                    st.time_ps = float(unquote(self.doc.raw(tm)))
                except ValueError:
                    st.time_ps = None
            mm = node.child("meta")
            st.meta_mode = unquote(self.doc.raw(mm)) if mm else ""
            mf = node.child("meta_file")
            st.meta_file = unquote(self.doc.raw(mf)) if mf else ""
            cf = node.child("cfg_file")
            st.cfg_file = unquote(self.doc.raw(cf)) if cf else ""
            st.is_production = bool(st.meta_file) or st.meta_mode.upper() == "FILE"
            self.stages.append(st)

    # -- access
    @property
    def production(self) -> Stage | None:
        prods = [s for s in self.stages if s.is_production]
        if prods:
            return prods[-1]
        sims = [s for s in self.stages if s.name == "simulate"]
        return sims[-1] if sims else None

    def simulate_stages(self) -> list[Stage]:
        return [s for s in self.stages if s.name == "simulate"]

    def total_time_ps(self) -> float:
        return sum(s.time_ps or 0.0 for s in self.stages)

    # -- editing
    def _set_in_stage(self, stage: Stage, key: str, text: str,
                      create: bool = True) -> bool:
        node = stage.node.child(key)
        if node is not None:
            self.doc.set_node(node, text)
        elif create:
            self.doc.insert_in_block(stage.node, f"{key} = {text}")
        else:
            return False
        self._scan()
        return True

    def set_meta_file(self, filename: str) -> bool:
        p = self.production
        if p is None:
            return False
        ok = self._set_in_stage(p, "meta_file", f'"{filename}"')
        p = self.production
        if p is not None and not p.meta_mode:
            self._set_in_stage(p, "meta", "FILE")
        return ok

    def set_cfg_file(self, filename: str) -> bool:
        p = self.production
        return self._set_in_stage(p, "cfg_file", f'"{filename}"') if p else False

    def set_stage_time(self, stage: Stage, ps: float) -> bool:
        return self._set_in_stage(stage, "time", _fmt_ps(ps))

    def set_stage_title(self, stage: Stage, title: str) -> bool:
        return self._set_in_stage(stage, "title", f'"{title}"')

    def set_production_jobname(self, name: str) -> bool:
        p = self.production
        return self._set_in_stage(p, "jobname", f'"{name}"') if p else False

    # -- validation
    def validate(self, pot_name: str = "", cfg_name: str = "",
                 spec: FunnelSpec | None = None,
                 cfg_time: float | None = None) -> list[Issue]:
        out: list[Issue] = []
        p = self.production
        if p is None:
            out.append(Issue("error", "msj", "No `simulate` stage found", ""))
            return out
        if p.meta_mode.upper() != "FILE":
            out.append(Issue("error", "msj",
                             f"Production stage has meta = {p.meta_mode or '<unset>'}",
                             "A custom funnel CV needs `meta = FILE` plus "
                             "`meta_file = \"<file>.pot\"`."))
        if not p.meta_file:
            out.append(Issue("error", "msj",
                             "Production stage has no `meta_file`", ""))
        elif pot_name and os.path.basename(p.meta_file) != os.path.basename(pot_name):
            out.append(Issue("error", "msj",
                             f"`meta_file` points at {p.meta_file!r}, but the "
                             f"potential will be written as {pot_name!r}",
                             "Multisim would stage the wrong potential."))
        if cfg_name and p.cfg_file and \
                os.path.basename(p.cfg_file) != os.path.basename(cfg_name):
            out.append(Issue("error", "msj",
                             f"`cfg_file` points at {p.cfg_file!r} instead of "
                             f"{cfg_name!r}", ""))
        if not p.cfg_file:
            out.append(Issue("warning", "msj",
                             "Production stage has no `cfg_file`",
                             "The stage then inherits Multisim defaults instead "
                             "of the reviewed .cfg."))
        cw = p.node.child("checkpt.write_last_step")
        if cw is None or unquote(self.doc.raw(cw)).lower() not in ("yes", "true"):
            out.append(Issue("warning", "msj",
                             "Production stage does not write a final checkpoint",
                             "Add `checkpt.write_last_step = yes` so the run can "
                             "be extended."))
        if p.time_ps and cfg_time and abs(p.time_ps - cfg_time) > 1e-6:
            out.append(Issue("warning", "msj",
                             f"Stage `time` = {p.time_ps} ps overrides the .cfg "
                             f"time of {cfg_time} ps",
                             "Keep the production length in one place."))
        # equilibration before biasing
        eq = [s for s in self.simulate_stages() if not s.is_production]
        eq_time = sum(s.time_ps or 0.0 for s in eq)
        if eq_time < 100:
            out.append(Issue("warning", "msj",
                             f"Only {eq_time:g} ps of relaxation/equilibration "
                             "before the biased stage",
                             "A bound-start funnel run should be equilibrated "
                             "unbiased first."))
        else:
            out.append(Issue("ok", "msj",
                             f"{len(eq)} pre-production stages, {eq_time:g} ps "
                             "of relaxation and equilibration", ""))
        for s in eq:
            rn = s.node.child("restraints.new")
            if rn is not None and s is eq[-1]:
                out.append(Issue("warning", "msj",
                                 "The last stage before production still adds "
                                 "positional restraints",
                                 "The ligand must be free before the funnel "
                                 "bias starts."))
        if spec is not None and cfg_time:
            if spec.meta_first >= cfg_time:
                out.append(Issue("error", "msj",
                                 "No hills would be deposited: the potential's "
                                 f"`first` = {spec.meta_first} ps exceeds the "
                                 f"{cfg_time} ps production", ""))
        return out


# --------------------------------------------------------------------------
# .sh
# --------------------------------------------------------------------------
SH_TEMPLATE = """#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd)"
cd "${{SCRIPT_DIR}}"

: "${{SCHRODINGER:?SCHRODINGER ortam degiskeni tanimli degil}}"

JOBNAME="{jobname}"

exec "${{SCHRODINGER}}/utilities/multisim" \\
  -JOBNAME "${{JOBNAME}}" \\
  -HOST {host} \\
  -maxjob {maxjob} \\
  -cpu {cpu} \\
  -m "${{JOBNAME}}.msj" \\
  -c "${{JOBNAME}}.cfg" \\
  -description "{description}" \\
  "${{JOBNAME}}.cms" \\
  -mode {mode} \\
  -o "${{JOBNAME}}-out.cms" \\
  -lic {lic}
"""


class ShFile:
    """The multisim launcher; fields are patched in place when possible."""

    def __init__(self, text: str = "", path: str = ""):
        self.path = path
        self.text = text or ""
        self.jobname = ""
        self.host = "localhost"
        self.maxjob = "1"
        self.cpu = "1"
        self.description = ""
        self.mode = "umbrella"
        self.lic = "DESMOND_GPGPU:16"
        self.out = ""
        self._initial: dict[str, str] = {}
        self._reference_map: dict[str, str] = {}
        if text:
            self._scan()

    @classmethod
    def load(cls, path: str) -> "ShFile":
        with open(path) as fh:
            return cls(fh.read(), path)

    def _scan(self) -> None:
        t = self.text
        values = shell_assignments(t)
        if values.get("JOBNAME") and "$" not in values["JOBNAME"]:
            self.jobname = values["JOBNAME"]
        else:
            m = re.search(r'-JOBNAME\s+("[^"\n]*"|\'[^\'\n]*\'|[^\s\\]+)', t)
            if m and "$" not in unquote(m.group(1)):
                self.jobname = unquote(m.group(1))
        for flag, attr in (("HOST", "host"), ("maxjob", "maxjob"),
                           ("cpu", "cpu"), ("mode", "mode"), ("lic", "lic")):
            m = re.search(rf'-{flag}\s+("[^"\n]*"|\'[^\'\n]*\'|[^\s\\]+)', t)
            if m:
                setattr(self, attr, unquote(m.group(1)))
        m = re.search(r'-description\s+"([^"]*)"', t)
        if m:
            self.description = m.group(1)
        m = re.search(r'-o\s+"([^"]*)"', t)
        if m:
            self.out = m.group(1)
        self._initial = {k: getattr(self, k) for k in
                         ("jobname", "host", "maxjob", "cpu", "description",
                          "mode", "lic", "out")}

    def rewrite_references(self, replacements: dict[str, str]) -> None:
        """Remember imported literal paths without changing shell structure."""
        self._reference_map.update(replacements)

    def render(self) -> str:
        """Patch the loaded script, or generate one from the template."""
        if not self.text:
            return SH_TEMPLATE.format(
                jobname=_shell_quote(self.jobname)[1:-1],
                host=shlex.quote(self.host), maxjob=shlex.quote(self.maxjob),
                cpu=shlex.quote(self.cpu), description=_shell_quote(self.description)[1:-1],
                mode=shlex.quote(self.mode), lic=shlex.quote(self.lic))
        t = self.text
        if self.jobname != self._initial.get("jobname", self.jobname):
            t, n = re.subn(
                r'^(\s*(?:export\s+)?JOBNAME=)("[^"\n]*"|\'[^\'\n]*\'|[^\s#]+)',
                lambda m: m.group(1) + _shell_quote(self.jobname), t, count=1,
                flags=re.M)
            if not n:
                t = re.sub(r'(-JOBNAME\s+)("[^"\n]*"|\'[^\'\n]*\'|[^\s\\]+)',
                           lambda m: m.group(1) + _shell_quote(self.jobname),
                           t, count=1)
        for flag, value in (("HOST", self.host), ("maxjob", self.maxjob),
                            ("cpu", self.cpu), ("mode", self.mode),
                            ("lic", self.lic)):
            attr = {"HOST": "host", "maxjob": "maxjob", "cpu": "cpu",
                    "mode": "mode", "lic": "lic"}[flag]
            if value == self._initial.get(attr, value):
                continue
            t = re.sub(rf'(-{flag}\s+)("[^"\n]*"|\'[^\'\n]*\'|[^\s\\]+)',
                       lambda m, v=value: m.group(1) + shlex.quote(v), t, count=1)
        if self.description != self._initial.get("description", self.description):
            t = re.sub(r'(-description\s+)("[^"\n]*"|\'[^\'\n]*\'|[^\s\\]+)',
                       lambda m: m.group(1) + _shell_quote(self.description),
                       t, count=1)
        return rewrite_file_references(t, self._reference_map)

    def validate(self, jobname: str = "") -> list[Issue]:
        out: list[Issue] = []
        t = self.render()
        if "multisim" not in t:
            out.append(Issue("error", "sh", "The script does not call multisim",
                             ""))
        if jobname and self.jobname and self.jobname != jobname:
            out.append(Issue("error", "sh",
                             f"JOBNAME is {self.jobname!r} but the job files are "
                             f"named {jobname!r}",
                             "The potential writes $JOBNAME.kerseq / "
                             "$JOBNAME.cvseq, and multisim looks for "
                             "$JOBNAME.msj / .cfg / .cms."))
        if "${JOBNAME}.msj" not in t and ".msj" not in t:
            out.append(Issue("warning", "sh", "No .msj passed with -m", ""))
        if "SCHRODINGER" not in t:
            out.append(Issue("warning", "sh",
                             "$SCHRODINGER is not checked", ""))
        if self.mode and self.mode != "umbrella":
            out.append(Issue("info", "sh", f"multisim -mode {self.mode}", ""))
        if "GPGPU" not in self.lic:
            out.append(Issue("info", "sh",
                             f"License token {self.lic!r} is not a GPU token",
                             "Long metadynamics runs are normally GPU jobs."))
        return out


# --------------------------------------------------------------------------
# bundle
# --------------------------------------------------------------------------
@dataclass
class BundlePaths:
    cms: str = ""
    pot: str = ""
    msj: str = ""
    cfg: str = ""
    sh: str = ""
    root_dir: str = ""
    archive_path: str = ""
    files: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def any(self) -> bool:
        return any((self.cms, self.pot, self.msj, self.cfg, self.sh))


_JOB_EXTS = {".cms": "cms", ".mae": "cms", ".maegz": "cms",
             ".pot": "pot", ".msj": "msj", ".cfg": "cfg", ".sh": "sh"}
_EXTRACTED: list[str] = []


@atexit.register
def _cleanup_bundle_archives() -> None:
    for directory in _EXTRACTED:
        shutil.rmtree(directory, ignore_errors=True)


def _shell_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace(
        "$", "\\$").replace("`", "\\`") + '"'


def shell_assignments(text: str) -> dict[str, str]:
    """Read simple literal shell variables; never execute imported scripts."""
    result: dict[str, str] = {}
    pattern = re.compile(
        r'^\s*(?:export\s+)?([A-Za-z_]\w*)=("[^"\n]*"|\'[^\'\n]*\'|[^\s#]+)',
        re.M)
    for match in pattern.finditer(text):
        value = unquote(match.group(2))
        if "$(" in value or "`" in value:
            continue
        result[match.group(1)] = value
    for _ in range(12):
        changed = False
        for key, value in result.items():
            expanded = re.sub(r'\$\{(\w+)\}|\$(\w+)',
                              lambda m: result.get(m.group(1) or m.group(2),
                                                   m.group(0)), value)
            if expanded != value:
                result[key] = expanded
                changed = True
        if not changed:
            break
    return result


def rewrite_file_references(text: str, replacements: dict[str, str]) -> str:
    """Replace complete filenames, preserving quotes and unrelated text."""
    if not replacements:
        return text
    keys = sorted((k for k, v in replacements.items() if k and k != v),
                  key=len, reverse=True)
    if not keys:
        return text
    # Bare job stems are patched only as entire quoted values (STEM="run")
    # so a generic basename such as "run" cannot rename Python identifiers.
    filenames = [k for k in keys if '.' in k or '/' in k or k.endswith('_trj')]
    stems = [k for k in keys if k not in filenames]
    pieces = []
    if filenames:
        pieces.append(r'(?<![\w./-])(?:' + '|'.join(re.escape(k) for k in filenames) + r')(?![\w.-])')
    if stems:
        pieces.append(r'(?<=["\'])(?:' + '|'.join(re.escape(k) for k in stems) + r')(?=["\'])')
    return re.sub('|'.join(pieces), lambda m: replacements[m.group(0)], text)


def _bundle_files(directory: str, recursive: bool) -> list[str]:
    result = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and
                         d not in ("__pycache__", "__MACOSX") and
                         not os.path.islink(os.path.join(root, d)))
        for name in sorted(names):
            if name.startswith('.') or name.endswith(('.pyc', '.zip')):
                continue
            path = os.path.join(root, name)
            if os.path.isfile(path):
                result.append(path)
        if not recursive:
            break
    return result


def _launcher_inputs(text: str) -> dict[str, str]:
    values = shell_assignments(text)
    inputs = {key.lower(): value for key, value in values.items()
              if key in ("CMS", "POT", "MSJ", "CFG") and "$" not in value}
    flat = text.replace("\\\n", " ")
    commands = [line for line in flat.splitlines()
                if not line.lstrip().startswith('#') and 'multisim' in line
                and re.search(r'(?<!\S)-m\s', line)]
    command = commands[-1] if commands else ''
    for flag, attr in (("m", "msj"), ("c", "cfg")):
        m = re.search(rf'(?<!\S)-{flag}\s+("[^"\n]*"|\'[^\'\n]*\'|[^\s\\]+)', command)
        if m:
            value = unquote(m.group(1))
            value = re.sub(r'\$\{(\w+)\}|\$(\w+)',
                           lambda x: values.get(x.group(1) or x.group(2),
                                                x.group(0)), value)
            if "$" not in value:
                inputs[attr] = value
    if "cms" not in inputs:
        # Positional .cms input; exclude the -o output argument.
        for m in re.finditer(r'("[^"\n]*\.cms"|\'[^\'\n]*\.cms\'|[^\s"\']+\.cms)', command):
            if re.search(r'-o\s*$', command[:m.start()]):
                continue
            value = unquote(m.group(1))
            value = re.sub(r'\$\{(\w+)\}|\$(\w+)',
                           lambda x: values.get(x.group(1) or x.group(2),
                                                x.group(0)), value)
            if "$" not in value and "=" not in value:
                inputs["cms"] = value
                break
    return inputs


def discover_bundle(path: str) -> BundlePaths:
    """Discover a job by references, accepting a file, directory or ZIP.

    Explicit input paths and stage/launcher references outrank basenames.
    An ambiguous archive is reported instead of combining unrelated jobs.
    """
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    archive = ""
    extracted = ""
    if os.path.isfile(path) and path.lower().endswith('.zip'):
        archive = path
        extracted = tempfile.mkdtemp(prefix="funnelforge_bundle_")
        try:
            # Solvated CMS structures routinely exceed the text-source cap;
            # retain the archive's total cap while accepting one large CMS.
            safe_extract_zip(path, extracted, limits=replace(
                DEFAULT_LIMITS, max_file_bytes=DEFAULT_LIMITS.max_archive_bytes))
        except BaseException:
            shutil.rmtree(extracted, ignore_errors=True)
            raise
        _EXTRACTED.append(extracted)
        path = extracted
    is_directory = os.path.isdir(path)
    directory = path if is_directory else os.path.dirname(path)
    files = _bundle_files(directory, recursive=is_directory)
    if extracted and files:
        directory = os.path.commonpath([os.path.dirname(p) for p in files])
    candidates = {attr: [p for p in files
                        if _JOB_EXTS.get(os.path.splitext(p)[1].lower()) == attr]
                  for attr in ("cms", "pot", "msj", "cfg", "sh")}
    out = BundlePaths(root_dir=directory, archive_path=archive)
    explicit = _JOB_EXTS.get(os.path.splitext(path)[1].lower()) if not is_directory else None
    if explicit:
        setattr(out, explicit, path)
    stem = os.path.splitext(path)[0] if explicit else ""
    for attr, choices in candidates.items():
        if getattr(out, attr):
            continue
        same = [p for p in choices if os.path.splitext(p)[0] == stem]
        if len(same) == 1:
            setattr(out, attr, same[0])

    def resolve(value: str, source: str) -> str:
        if not value or "$" in value or value == "?":
            return ""
        for root in (os.path.dirname(source), directory):
            candidate = os.path.abspath(os.path.join(root, value))
            if os.path.isfile(candidate):
                return candidate
        return ""

    # Find the launcher/MSJ connected to a selected potential, even if its
    # basename differs. This prevents choosing a neighbouring production job.
    for attr in ("msj", "sh"):
        if getattr(out, attr):
            continue
        matches = []
        for candidate in candidates[attr]:
            if attr == "msj":
                parsed = MsjFile.load(candidate).production
                refs = {"pot": parsed.meta_file, "cfg": parsed.cfg_file} if parsed else {}
            else:
                refs = _launcher_inputs(read_text_file(candidate)[0])
            if any(getattr(out, key) and resolve(val, candidate) == getattr(out, key)
                   for key, val in refs.items()):
                matches.append(candidate)
        if len(matches) == 1:
            setattr(out, attr, matches[0])
        elif len(candidates[attr]) == 1:
            setattr(out, attr, candidates[attr][0])

    for _ in range(2):
        if out.sh:
            refs = _launcher_inputs(read_text_file(out.sh)[0])
            for attr, value in refs.items():
                found = resolve(value, out.sh)
                if found and attr != explicit:
                    setattr(out, attr, found)
                elif not found:
                    message = f"Launcher input could not be resolved: {value}"
                    if message not in out.messages:
                        out.messages.append(message)
        if out.msj:
            production = MsjFile.load(out.msj).production
            if production:
                for attr, value in (("pot", production.meta_file),
                                    ("cfg", production.cfg_file)):
                    found = resolve(value, out.msj)
                    if found and attr != explicit:
                        setattr(out, attr, found)
                    elif value and not found:
                        message = f"Production input could not be resolved: {value}"
                        if message not in out.messages:
                            out.messages.append(message)
    for attr, choices in candidates.items():
        if not getattr(out, attr):
            if len(choices) == 1:
                setattr(out, attr, choices[0])
            elif len(choices) > 1:
                names = ', '.join(os.path.relpath(p, directory) for p in choices)
                raise ValueError(f"Multiple {attr} inputs found ({names}); open the desired job file explicitly.")
    if not out.any():
        raise FileNotFoundError(f"No Desmond job files found in {archive or path}")
    # For an individual input, bring sidecar subdirectories only from a
    # recognisable package; do not recursively copy arbitrary parent folders.
    package = bool(archive or is_directory or any(
        os.path.basename(p).lower().endswith('.sha256') for p in files))
    if package:
        files = _bundle_files(directory, recursive=True)
    main = {getattr(out, attr) for attr in candidates if getattr(out, attr)}
    out.files = sorted(set(files) | main)
    return out


def bundle_replacements(paths: BundlePaths, jobname: str) -> dict[str, str]:
    """Map source-relative and literal input paths to exported filenames."""
    replacements = {}
    for attr in ("cms", "pot", "msj", "cfg", "sh"):
        source = getattr(paths, attr)
        if not source:
            continue
        target = f"{jobname}.{attr}"
        replacements[source] = target
        replacements[os.path.basename(source)] = target
        if paths.root_dir:
            relative = os.path.relpath(source, paths.root_dir)
            replacements[relative] = target
            replacements['./' + relative] = target
        stem = os.path.splitext(os.path.basename(source))[0]
        # Postprocessing scripts commonly store STEM = "<job>" and derive
        # all output paths from it. Keep these helpers usable after renaming.
        replacements[stem] = jobname
        for suffix in ('.cvseq', '.kerseq', '.cpt', '.ene', '-out.cms',
                       '_trj', '_simbox.dat', '_multisim.log', '-multisim_checkpoint'):
            replacements[stem + suffix] = jobname + suffix
    return replacements


def _relative_bundle_path(paths: BundlePaths, source: str) -> str:
    root = paths.root_dir or os.path.dirname(source)
    relative = os.path.relpath(source, root)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise ValueError(f"Package dependency lies outside its source directory: {source}")
    return relative


def stage_bundle_export(paths: BundlePaths, staging_dir: str,
                        replacements: dict[str, str]) -> list[str]:
    """Copy imported sidecars into a private export staging directory.

    Primary job files are written by Job.export; everything else remains
    available to custom launch/preflight/analysis scripts, including nested
    dependency directories. Imported scripts are never executed here.
    """
    main = {getattr(paths, attr) for attr in ("cms", "pot", "msj", "cfg", "sh")}
    written = []
    text_exts = {'.py', '.sh', '.msj', '.cfg', '.json', '.yaml', '.yml', '.txt'}
    for source in paths.files:
        if source in main:
            continue
        relative = _relative_bundle_path(paths, source)
        target = os.path.join(staging_dir, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.lexists(target):
            raise FileExistsError(f"Package dependency collides with an exported input: {relative}")
        shutil.copy2(source, target)
        # Frozen source validators keep their exact source provenance. They
        # are replaced in the launch path by finalize_bundle_export if needed.
        if os.path.splitext(source)[1].lower() in text_exts and \
                os.path.basename(source) != 'validate_package.py':
            original, enc = read_text_file(source)
            changed = rewrite_file_references(original, replacements)
            if changed != original:
                with open(target, 'wb') as fh:
                    fh.write(enc.encode(changed))
        if source.lower().endswith('.sh'):
            os.chmod(target, os.stat(target).st_mode | 0o100)
        written.append(target)
    return written


_EXPORT_VALIDATOR = r'''#!/usr/bin/env python3
"""Check this adjusted FunnelForge export before the engine preflight."""
import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent

def require(condition, message):
    if not condition:
        raise SystemExit("Export validation failed: " + message)

def local(name):
    p = ROOT / name
    require(not Path(name).is_absolute() and '..' not in Path(name).parts,
            "unsafe package path: " + name)
    require(p.is_file(), "missing input: " + name)
    return p

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def finite(value):
    if isinstance(value, dict):
        return all(finite(v) for v in value.values())
    if isinstance(value, list):
        return all(finite(v) for v in value)
    return not isinstance(value, (int, float)) or math.isfinite(value)

def main():
    report = json.loads(local('FUNNELFORGE_EXPORT.json').read_text(encoding='utf-8'))
    require(report.get('verified') is True, 'GUI numerical verification did not pass')
    require(finite(report), 'non-finite numerical metadata')
    manifest = local(report['manifest'])
    covered = set()
    for line in manifest.read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *](.+)', line)
        require(match is not None, 'malformed checksum line')
        name = match[2]
        require(name not in covered, 'duplicate checksum entry: ' + name)
        require(digest(local(name)) == match[1].lower(), 'checksum mismatch: ' + name)
        covered.add(name)
    for name in report['required_files']:
        require(name in covered, 'input missing from checksum manifest: ' + name)
    files = report['files']
    for attr, entry in files.items():
        require(digest(local(entry['path'])) == entry['sha256'], 'edited input: ' + attr)
    for attr in ('pot', 'cms'):
        require(re.fullmatch(r'[0-9a-f]{64}', report['source_hashes'].get(attr, '')),
                'source provenance hash missing for ' + attr)
    geometry = report['geometry']
    require(len(geometry['axis']) == 3 and len(geometry['origin']) == 3, 'invalid frame')
    norm = sum(v * v for v in geometry['axis']) ** .5
    require(abs(norm - 1.0) < 1e-7, 'axis is not normalized')
    require(geometry['z_min'] < geometry['z_max'] and geometry['r_cyl'] > 0,
            'invalid axial range or cylinder radius')
    for name in ('msj', 'cfg'):
        local(files[name]['path'])
    msj = local(files['msj']['path']).read_text(encoding='utf-8-sig')
    for key, attr in (('meta_file', 'pot'), ('cfg_file', 'cfg')):
        values = re.findall(r'\b' + key + r'\s*=\s*["\x27]?([^\s"\x27}]+)', msj)
        require(values and values[-1] == files[attr]['path'], 'stage reference mismatch: ' + key)
    print('PASS: adjusted export checksums, references, source provenance and numerical verification metadata.')
    print('Engine compatibility is checked next by the retained Desmond preflight.')

if __name__ == '__main__':
    main()
'''


def finalize_bundle_export(paths: BundlePaths, staging_dir: str,
                           replacements: dict[str, str], verification: dict,
                           jobname: str, geometry: dict | None = None) -> list[str]:
    """Refresh package integrity and replace invalidated frozen validators.

    Historical source geometry proofs remain byte-for-byte in provenance.
    The current export checker validates the adjusted artifacts and retains
    the existing engine-dependent preflight as the next launch step.
    """
    manifests = [p for p in paths.files if p.lower().endswith('.sha256')]
    staged_sh = os.path.join(staging_dir, f'{jobname}.sh')
    script = read_text_file(staged_sh)[0] if os.path.isfile(staged_sh) else ''
    changed = any(source and os.path.isfile(os.path.join(staging_dir, f'{jobname}.{attr}'))
                  and file_digest(source) != file_digest(os.path.join(staging_dir, f'{jobname}.{attr}'))
                  for attr in ('pot', 'cms', 'cfg', 'msj', 'sh')
                  for source in (getattr(paths, attr),))
    static_validators = [p for p in paths.files if os.path.basename(p) == 'validate_package.py']
    replacing = bool(changed and static_validators and 'validate_package.py' in script)
    current_checker = 'funnelforge_validate_export.py' in script
    written = []
    if replacing or current_checker:
        if replacing:
            provenance = os.path.join(staging_dir, '.funnelforge-source')
            for source in paths.files:
                if source == paths.cms:
                    continue
                target = os.path.join(provenance, _relative_bundle_path(paths, source))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(source, target)
                written.append(target)
            script = rewrite_file_references(script, {'validate_package.py': 'funnelforge_validate_export.py'})
            with open(staged_sh, 'w', encoding='utf-8', newline='') as fh:
                fh.write(script)
            written.append(staged_sh)
        validator = os.path.join(staging_dir, 'funnelforge_validate_export.py')
        with open(validator, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(_EXPORT_VALIDATOR)
        os.chmod(validator, 0o755)
        written.append(validator)
        manifest_name = 'PACKAGE_MANIFEST.sha256'
        metadata = {
            'format': 'FunnelForge adjusted export v1', 'jobname': jobname,
            'verified': bool(verification.get('ok')), 'verification': verification,
            'geometry': geometry or {}, 'manifest': manifest_name,
            'source_hashes': {a: file_digest(getattr(paths, a))
                              for a in ('pot', 'cms')},
            'files': {a: {'path': f'{jobname}.{a}',
                           'sha256': file_digest(os.path.join(staging_dir, f'{jobname}.{a}'))}
                      for a in ('pot', 'cms', 'msj', 'cfg', 'sh')
                      if os.path.isfile(os.path.join(staging_dir, f'{jobname}.{a}'))},
        }
        metadata['required_files'] = [v['path'] for v in metadata['files'].values()] + [
            'FUNNELFORGE_EXPORT.json', 'funnelforge_validate_export.py']
        record_path = os.path.join(staging_dir, 'FUNNELFORGE_EXPORT.json')
        with open(record_path, 'w', encoding='utf-8') as fh:
            json.dump(metadata, fh, indent=2, allow_nan=False)
            fh.write('\n')
        written.append(record_path)
        note = (
            '# Adjusted FunnelForge export\n\n'
            'The launcher checks the current export with funnelforge_validate_export.py, '
            'then retains the original Desmond preflight. The JSON record describes '
            'the exported geometry and numerical comparisons. Engine compatibility '
            'is established by the preflight at launch, not by a checksum.\n\n'
            'The imported validator and documentation lock the original geometry. '
            'Their geometry-preservation and convergence claims do not apply to '
            'this adjusted job. The original inputs and reports are retained in '
            '.funnelforge-source; the unchanged CMS is identified by its source hash. '
            'Reassess sampling and scientific suitability for the adjusted geometry.\n'
        )
        note_path = os.path.join(staging_dir, 'FUNNELFORGE_EXPORT_README.md')
        with open(note_path, 'w', encoding='utf-8') as fh:
            fh.write(note)
        written.append(note_path)
        if not any(os.path.basename(p) == manifest_name for p in manifests):
            manifests.append(os.path.join(paths.root_dir, manifest_name))
    # Refresh all imported hash manifests last. Every current artifact is
    # covered; historical provenance is covered too, without claiming it is
    # an updated validation of this geometry. Manifests cannot hash themselves.
    if manifests:
        all_files = []
        for root, dirs, names in os.walk(staging_dir):
            for name in sorted(names):
                target = os.path.join(root, name)
                if not name.lower().endswith('.sha256'):
                    all_files.append(target)
        for source in manifests:
            relative = _relative_bundle_path(paths, source)
            target = os.path.join(staging_dir, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'w', encoding='utf-8', newline='\n') as fh:
                for p in sorted(all_files):
                    name = os.path.relpath(p, staging_dir)
                    fh.write(f'{file_digest(p)}  {name}\n')
            written.append(target)
    return written


def validate_export_tree(directory: str, jobname: str,
                         complete: bool = True) -> list[Issue]:
    """Validate stage and launcher dependencies without running package code."""
    issues = []
    level = 'error' if complete else 'warning'

    def check(value: str, source: str) -> None:
        if not value or value == '?' or '$' in value:
            return
        target = os.path.abspath(os.path.join(os.path.dirname(source), value))
        if not os.path.isfile(target):
            issues.append(Issue(level, 'export',
                                f"Missing exported dependency: {value}",
                                f"Referenced by {os.path.basename(source)}."))
        elif os.path.isabs(value) or os.path.commonpath([directory, target]) != directory:
            issues.append(Issue(level, 'export',
                                f"Export references an external input: {value}",
                                "The exported package must contain its launch inputs."))

    msj = os.path.join(directory, f'{jobname}.msj')
    if os.path.isfile(msj):
        for stage in MsjFile.load(msj).stages:
            check(stage.meta_file, msj)
            check(stage.cfg_file, msj)
    sh = os.path.join(directory, f'{jobname}.sh')
    if os.path.isfile(sh):
        text = read_text_file(sh)[0]
        for value in _launcher_inputs(text).values():
            check(value, sh)
        values = shell_assignments(text)
        for key in ('VALIDATOR', 'PREFLIGHT', 'MANIFEST'):
            if key in values:
                check(values[key], sh)
    return issues


CMS_COPY, CMS_HARDLINK, CMS_SYMLINK, CMS_SKIP = "copy", "hardlink", "symlink", "skip"


def place_cms(src: str, dst: str, mode: str) -> str:
    """Put the structure next to the job files without duplicating 50 MB."""
    if mode == CMS_SKIP or not src:
        return ""
    if os.path.abspath(src) == os.path.abspath(dst):
        return dst
    if os.path.exists(dst) or os.path.islink(dst):
        os.remove(dst)
    if mode == CMS_HARDLINK:
        try:
            os.link(src, dst)
            return dst
        except OSError:
            pass
    if mode == CMS_SYMLINK:
        os.symlink(os.path.abspath(src), dst)
        return dst
    shutil.copy2(src, dst)
    return dst
