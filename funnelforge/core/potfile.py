"""Read and write the Desmond ``.pot`` M-expression for a funnel WT-MetaD run.

The reader recovers a :class:`~funnelforge.core.funnel.FunnelSpec` from an
existing potential file and remembers the exact textual form of every literal
and comment it saw.  The writer emits the canonical layout, re-using those
literals for any value the user did not touch, so importing and exporting an
untouched file reproduces it byte for byte.
"""

from __future__ import annotations

import json
import re

from .funnel import FunnelSpec, Selection, Diagnostic, DEFAULT_PRINTS
from .terms import Term, EmitCtx, emit_term

EPS_LITERAL = "1.0e-12"

# --------------------------------------------------------------------------
# number formatting
# --------------------------------------------------------------------------


def fmt_number(value: float) -> str:
    """Shortest exact decimal form, always with a decimal point."""
    if value is None:
        return "0.0"
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        return "0.0"
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v)}.0"
    s = repr(v)
    if "e" in s or "E" in s:
        mant, _, exp = s.partition("e")
        if "." not in mant:
            mant += ".0"
        s = f"{mant}e{exp}"
    return s


def fmt_g(value: float) -> str:
    """Compact form for prose (gamma=15, 310 K)."""
    v = float(value)
    if v == int(v) and abs(v) < 1e12:
        return str(int(v))
    return f"{v:g}"


class Literals:
    """Emit imported literals verbatim while a value is still untouched."""

    def __init__(self, spec: FunnelSpec):
        self.spec = spec

    def __call__(self, key: str, value: float) -> str:
        txt = self.spec.literals.get(key)
        if txt is not None:
            old = self.spec.literal_values.get(key)
            if old is not None and float(value) == float(old):
                return txt
        return fmt_number(value)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_ID = r"[A-Za-z_]\w*"

_RE_DECLARE = re.compile(r"declare_(meta|output)\s*\((.*?)\)\s*;", re.S)
_RE_ATOMSEL = re.compile(rf"^\s*({_ID})\s*=\s*atomsel\s*\(\s*\"(.*?)\"\s*\)\s*;",
                         re.M)
_RE_SCALAR = re.compile(rf"^\s*({_ID})\s*=\s*({_NUM})\s*;", re.M)
_RE_ORIGIN = re.compile(rf"^\s*origin\s*=\s*core_com\s*\+\s*({_NUM})\s*\*\s*"
                        rf"(axis_raw|axis)\s*"
                        rf"(?:([-+])\s*({_NUM})\s*\*\s*e1\s*)?"
                        rf"(?:([-+])\s*({_NUM})\s*\*\s*e2\s*)?;", re.M)
_RE_META = re.compile(r"(\w+)\s*=\s*meta\s*\(\s*(\d+)\s*,\s*array\s*\((.*?)\)"
                      r"\s*,\s*array\s*\((.*?)\)\s*\)\s*;", re.S)
_RE_PRINT = re.compile(rf"print\s*\(\s*\"(.*?)\"\s*,\s*({_ID})\s*\)\s*;")
_RE_COM = re.compile(rf"^\s*({_ID})\s*=\s*center_of_mass\s*\(\s*({_ID})\s*\)\s*;",
                     re.M)
_RE_DIST = re.compile(rf"^\s*({_ID})\s*=\s*norm\s*\(\s*min_image\s*\(\s*"
                      rf"lig_com\s*-\s*({_ID})\s*\)\s*\)\s*;", re.M)
_RE_HILL = re.compile(rf"^\s*hill\s*=\s*({_ID})\s*\*\s*exp\s*\(\s*({_ID})\s*/\s*"
                      rf"\(\s*-\s*({_ID})\s*\)\s*\)\s*;", re.M)
_RE_RALLOWED = re.compile(r"^\s*r_allowed\s*=\s*if\s+(.*?)\s+then\s+(.*?)\s+"
                          r"else\s+(.*?)\s*;", re.M)
_RE_TERM_MARKER = re.compile(r"^\s*#\s*@ff-term\s+(\{.*\})\s*$", re.M)
_RE_AXIS_MARKER = re.compile(r"^\s*#\s*@ff-axis\s+(\{.*\})\s*$", re.M)
_RE_AXIS_TILT = re.compile(rf"^\s*axis\s*=\s*({_NUM})\s*\*\s*axis0\s*"
                           rf"(?:([-+])\s*({_NUM})\s*\*\s*p1\s*)?"
                           rf"(?:([-+])\s*({_NUM})\s*\*\s*p2\s*)?;", re.M)
_RE_TOTAL = re.compile(r"^\s*v_total\s*=\s*([^;]+);", re.M)


class ParseReport:
    """What the reader understood, and what it did not."""

    def __init__(self):
        self.recognised: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.unknown_statements: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        bits = [f"{len(self.recognised)} constructs recognised"]
        if self.unknown_statements:
            bits.append(f"{len(self.unknown_statements)} statements not modelled")
        if self.warnings:
            bits.append(f"{len(self.warnings)} warnings")
        if self.errors:
            bits.append(f"{len(self.errors)} errors")
        return ", ".join(bits)


def _split_args(text: str) -> list[str]:
    out, depth, cur = [], 0, ""
    in_str = False
    for ch in text:
        if ch == '"':
            in_str = not in_str
            cur += ch
            continue
        if not in_str:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                out.append(cur.strip())
                cur = ""
                continue
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def parse_asl_atoms(asl: str) -> list[int]:
    """Read an explicit 1-based atom list without discarding bad tokens.

    General ASL belongs to the topology-aware selection resolver.  A malformed
    index list must never silently become a different, smaller atom group.
    """
    body = asl.strip()
    m = re.match(r"^(?:(?:atom|a)\s*\.\s*(?:num|n)?\s*|atom\s+|index\s+)(.*)$", body, re.I | re.S)
    if m:
        body = m.group(1)
    elif not re.fullmatch(r"[\d,\s-]*", body):
        raise ValueError("Not an explicit atom-index selection")
    out: list[int] = []
    for part in re.split(r"[,\s]+", body.strip()):
        if not part:
            continue
        if not re.fullmatch(r"\d+(?:-\d+)?", part):
            raise ValueError(f"Malformed atom-index token: {part!r}")
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError as exc:
                raise ValueError(f"Malformed atom-index range: {part!r}") from exc
            if lo < 1 or hi < lo:
                raise ValueError(f"Invalid 1-based atom-index range: {part!r}")
            out.extend(range(lo, hi + 1))
        else:
            value = int(part)
            if value < 1:
                raise ValueError("Atom indices must be positive and 1-based")
            out.append(value)
    return out


def strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in text.splitlines())


def _collect_comments(text: str) -> dict[str, list[str]]:
    """Attach each run of comment lines to the statement that follows it."""
    notes: dict[str, list[str]] = {}
    run: list[str] = []
    seen_stmt = False
    classifiers = [
        (re.compile(r"^\s*declare_meta"), "header"),
        (re.compile(r"^\s*declare_output"), "output"),
        (re.compile(r"^\s*lig\s*=\s*atomsel"), "lig"),
        (re.compile(r"^\s*(site|core)\s*=\s*atomsel"), "frame"),
        (re.compile(r"^\s*lig_com\s*="), "com"),
        (re.compile(r"^\s*axis_raw\s*="), "axis"),
        (re.compile(r"^\s*fref\s*=\s*atomsel"), "perpframe"),
        (re.compile(r"^\s*origin\s*="), "origin"),
        (re.compile(r"^\s*z_cc\s*="), "funnel"),
        (re.compile(r"^\s*rad_excess\s*="), "rad"),
        (re.compile(r"^\s*z_min\s*="), "zwall"),
        (re.compile(r"^\s*ktemp\s*="), "wt"),
        (re.compile(r"^\s*\w+_sel\s*=\s*atomsel"), "diag"),
        (re.compile(r"^\s*v_total\s*="), "total"),
        (re.compile(r"^\s*print\s*\("), "print"),
    ]
    term_run = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            if s.startswith("# @ff-term") or s.startswith("# @ff-axis"):
                # the run belongs to a shape term, which carries its own
                # comments; it must not be re-attached to the next statement
                term_run = True
            run.append(s)
            continue
        if not s:
            continue
        key = None
        for rx, k in classifiers:
            if rx.match(ln):
                key = k
                break
        if run and term_run:
            run = []
            term_run = False
        if run:
            if not seen_stmt:
                key = "header"
            if key and key not in notes:
                notes[key] = run
            elif key:
                notes[key].extend(run)
            elif "loose" not in notes:
                notes["loose"] = run
            run = []
        seen_stmt = True
    if run:
        notes["footer"] = run
    return notes


def parse_pot(text: str, path: str = "") -> tuple[FunnelSpec, ParseReport]:
    rep = ParseReport()
    spec = FunnelSpec()
    spec.source_path = path
    spec.source_text = text
    code = strip_comments(text)

    # ---- comments
    notes = _collect_comments(text)
    spec.header_comment = notes.pop("header", [])
    spec.notes = notes

    # ---- declare_meta / declare_output
    got_meta = False
    for kind, args in _RE_DECLARE.findall(text):
        kv: dict[str, str] = {}
        for a in _split_args(strip_comments(args)):
            if "=" not in a:
                continue
            k, _, v = a.partition("=")
            kv[k.strip()] = v.strip()
        if kind == "meta":
            got_meta = True
            if "dimension" in kv:
                spec.dimension = int(float(kv["dimension"]))
            for key, attr in (("cutoff", "kernel_cutoff"),
                              ("first", "meta_first"),
                              ("interval", "meta_interval")):
                if key in kv:
                    _set_num(spec, attr, kv[key])
            if "name" in kv:
                spec.kerseq_name = kv["name"].strip('"')
            if "initial" in kv:
                spec.initial_kernels = kv["initial"].strip('"')
            rep.recognised.append("declare_meta")
        else:
            if "name" in kv:
                spec.cvseq_name = kv["name"].strip('"')
            for key, attr in (("first", "cv_first"), ("interval", "cv_interval")):
                if key in kv:
                    _set_num(spec, attr, kv[key])
            rep.recognised.append("declare_output")
    if not got_meta:
        rep.errors.append("No declare_meta(...) block found - this is not a "
                          "metadynamics potential file.")

    # ---- atom selections
    sels: dict[str, tuple[str, list[int]]] = {}
    for var, asl in _RE_ATOMSEL.findall(code):
        try:
            ids = parse_asl_atoms(asl)
        except ValueError:
            ids = []
        sels[var] = (asl, ids)
    if "fref" in sels:
        asl, idx = sels.pop("fref")
        spec.frame_ref = Selection("fref", idx)
        rep.recognised.append(f"frame reference ({len(idx)} atoms)")
    for var, target in (("lig", "lig"), ("site", "site"), ("core", "core")):
        if var in sels:
            asl, idx = sels.pop(var)
            sel = Selection(var, idx)
            setattr(spec, target, sel)
            rep.recognised.append(f"{var} selection ({len(idx)} atoms)")
        else:
            rep.errors.append(f"Missing `{var} = atomsel(...)` statement.")

    # ---- diagnostics: <var>_sel = atomsel(...)  +  d_x = norm(min_image(...))
    com_of = {a: b for a, b in _RE_COM.findall(code)}
    dist_of: dict[str, str] = {}       # com-var -> distance-var
    for dvar, comvar in _RE_DIST.findall(code):
        dist_of[comvar] = dvar
    for var, (asl, idx) in list(sels.items()):
        if not var.endswith("_sel"):
            continue
        base = var[:-4]
        if com_of.get(base) != var:
            continue
        d = Diagnostic(var=base, label=_label_for(base, text),
                       indices=idx, dist_var=dist_of.get(base, "d_" + base))
        spec.diagnostics.append(d)
        sels.pop(var)
        rep.recognised.append(f"diagnostic {d.label} ({len(idx)} atoms)")
    for var in sels:
        rep.warnings.append(f"Unused atomsel variable `{var}` was kept out of "
                            "the model and will not be re-emitted.")

    # ---- origin
    m = _RE_ORIGIN.search(code)
    if m:
        val = float(m.group(1))
        if m.group(4):
            spec.origin_lat1 = float(m.group(4)) * (
                -1.0 if m.group(3) == "-" else 1.0)
        if m.group(6):
            spec.origin_lat2 = float(m.group(6)) * (
                -1.0 if m.group(5) == "-" else 1.0)
        if m.group(2) == "axis_raw":
            spec.origin_mode = "fraction"
            spec.origin_frac = val
            spec.literals["origin_frac"] = m.group(1)
            spec.literal_values["origin_frac"] = val
        else:
            spec.origin_mode = "absolute"
            spec.origin_offset = val
            spec.literals["origin_offset"] = m.group(1)
            spec.literal_values["origin_offset"] = val
        rep.recognised.append("funnel origin")
    else:
        rep.errors.append("Could not read the `origin = core_com + f*axis_raw` "
                          "statement.")

    # ---- scalars
    scalars = {name: lit for name, lit in _RE_SCALAR.findall(code)}
    wanted = {"z_cc": "z_cc", "r_cyl": "r_cyl", "cone_slope": "cone_slope",
              "k_rad": "k_rad", "z_min": "z_min", "z_max": "z_max",
              "k_z": "k_z", "ktemp": "ktemp", "h0": "h0",
              "sigma_z": "sigma_z", "sigma_rho": "sigma_rho"}
    funnel_only = {"z_cc", "r_cyl", "cone_slope", "k_rad", "z_min", "z_max",
                   "k_z"}
    have_funnel = "z_cc" in scalars and "r_cyl" in scalars
    for name, attr in wanted.items():
        if name in scalars:
            _set_num(spec, attr, scalars[name])
            rep.recognised.append(name)
        elif name == "sigma_rho" or (name in funnel_only and not have_funnel):
            continue
        else:
            rep.warnings.append(f"`{name}` not found; keeping the default "
                                f"{getattr(spec, attr)}.")
    if not have_funnel:
        spec.funnel_enabled = False

    # ---- a tilted funnel axis: the marker is exact, the constants are the
    #      fallback for a hand-written file
    m = _RE_AXIS_MARKER.search(text)
    if m:
        try:
            data = json.loads(m.group(1))
            spec.axis_tilt_deg = float(data.get("tilt_deg", 0.0))
            spec.axis_azimuth_deg = float(data.get("azimuth_deg", 0.0))
            rep.recognised.append("tilted funnel axis")
        except Exception as exc:
            rep.warnings.append(f"An @ff-axis marker could not be read ({exc}).")
            m = None
    if m is None:
        m = _RE_AXIS_TILT.search(code)
    else:
        m = None
    if m:
        import math as _math
        ca = float(m.group(1))
        cb = float(m.group(3) or 0.0) * (-1.0 if m.group(2) == "-" else 1.0)
        cc = float(m.group(5) or 0.0) * (-1.0 if m.group(4) == "-" else 1.0)
        spec.axis_tilt_deg = _math.degrees(_math.atan2(_math.hypot(cb, cc),
                                                       ca))
        spec.axis_azimuth_deg = _math.degrees(_math.atan2(cc, cb))
        rep.recognised.append("tilted funnel axis")

    # ---- extra shape terms, recovered from their marker comments
    for payload in _RE_TERM_MARKER.findall(text):
        try:
            data = json.loads(payload)
            spec.terms.append(Term.from_dict(data))
        except Exception as exc:
            rep.errors.append(f"A @ff-term marker could not be read ({exc}); "
                              "the term would be lost on export.")
    if spec.terms:
        rep.recognised.append(f"{len(spec.terms)} shape term(s)")

    # ---- what the total is actually made of
    m = _RE_TOTAL.search(code)
    if m:
        contribs = [c.strip() for c in m.group(1).split("+") if c.strip()]
        spec.funnel_enabled = "v_rad" in contribs or "v_z" in contribs
        known_contrib = {"v_rad", "v_z", "v_meta"}
        known_contrib |= {t.energy_var for t in spec.terms}
        unknown = [c for c in contribs if c not in known_contrib]
        if unknown:
            rep.errors.append(
                "`v_total` adds " + ", ".join(f"`{u}`" for u in unknown)
                + " which this interface does not model. Exporting would drop "
                  "that contribution and change the physics - edit the file by "
                  "hand instead.")
        for t in spec.terms:
            if t.enabled and t.energy_var not in contribs:
                rep.warnings.append(
                    f"Term `{t.label or t.var}` is marked enabled but does not "
                    "appear in v_total; it will be added on export.")
            if not t.enabled and t.energy_var in contribs:
                t.enabled = True
        rep.recognised.append("v_total decomposition")
    else:
        rep.warnings.append("No `v_total = ...` statement was found.")

    # ---- r_allowed template check
    m = _RE_RALLOWED.search(code)
    if m and spec.funnel_enabled:
        cond, then, els = (x.replace(" ", "") for x in m.groups())
        if cond != "z_cc-z" or "cone_slope" not in then:
            rep.warnings.append("`r_allowed` deviates from the cone/cylinder "
                                "template; the exported file will use the "
                                "canonical form.")
    elif spec.funnel_enabled:
        rep.warnings.append("`r_allowed` statement not recognised.")

    # ---- meta() calls
    metas = _RE_META.findall(code)
    if metas:
        for var, mid, sig_args, cv_args in metas:
            cvs = [c.strip() for c in _split_args(cv_args)]
            sigs = [s.strip() for s in _split_args(sig_args)]
            if var == "v_meta":
                spec.dimension = max(1, len(cvs))
                if len(sigs) >= 2:
                    # sigmas follow the hill height
                    if len(sigs) >= 3 and spec.dimension >= 2:
                        _set_num(spec, "sigma_rho", sigs[2]) \
                            if _is_num(sigs[2]) else None
                if len(cvs) >= 2 and cvs[1] != "rho":
                    rep.warnings.append(
                        f"Second metadynamics CV is `{cvs[1]}`; the interface "
                        "models z and rho only.")
        rep.recognised.append(f"{len(metas)} meta() call(s)")
    else:
        rep.errors.append("No meta(...) call found; nothing would be biased.")

    # ---- well tempered wiring
    m = _RE_HILL.search(code)
    if m and (m.group(1) != "h0" or m.group(3) != "ktemp"):
        rep.warnings.append("The hill-height expression is non-standard; the "
                            "canonical h0*exp(v_old/(-ktemp)) will be written.")
    elif not m:
        rep.warnings.append("No `hill = h0*exp(v_old/(-ktemp))` statement found; "
                            "the export will add the standard well-tempered "
                            "hill scaling.")

    # ---- prints
    prints = _RE_PRINT.findall(code)
    if prints:
        spec.prints = [(lbl, var) for lbl, var in prints]
        rep.recognised.append(f"{len(prints)} print statement(s)")
    else:
        spec.prints = list(DEFAULT_PRINTS)

    # ---- gamma / temperature from the comment, else defaults
    gam, temp = _gamma_from_comment(text)
    if temp:
        spec.temperature = temp
    if gam:
        spec.gamma = gam
    else:
        spec.gamma = spec.gamma_from_ktemp()
    if spec.temperature > 0:
        implied = spec.gamma_from_ktemp()
        if gam and abs(implied - gam) > 5e-3:
            rep.warnings.append(
                f"kTemp = {spec.ktemp} implies gamma = {implied:.4f}, but the "
                f"file comment says gamma = {fmt_g(gam)}.")

    # ---- ligand label
    lm = re.search(r"#\s*Target ligand:\s*(\S+)\s+chain", text)
    if lm:
        spec.ligand_label = lm.group(1)

    # ---- statements we did not model
    body = _RE_DECLARE.sub("", code)
    for ln in body.splitlines():
        s = ln.strip().rstrip(";").strip()
        if not s:
            continue
        if re.match(r"^(declare_meta|declare_output|print)\b", s):
            continue
        if "=" in s:
            lhs = s.split("=", 1)[0].strip()
            term_lhs = any(lhs == t.energy_var or lhs.startswith(t.var + "_")
                           for t in spec.terms)
            if lhs in _KNOWN_LHS or lhs in {d.var for d in spec.diagnostics} \
               or lhs in {d.dist_var for d in spec.diagnostics} \
               or lhs.endswith("_sel") or term_lhs:
                continue
            rep.unknown_statements.append(s)
        elif s not in ("v_total", ")"):
            if not s.startswith(")") and "declare" not in s:
                rep.unknown_statements.append(s)
    # The template reader is retained for native designer files.  House-style
    # frames, additional forces and custom expressions use a lossless source
    # adapter instead of being normalised into a different physical model.
    custom = (bool(rep.errors or rep.unknown_statements) or any(
        fragment in warning for warning in rep.warnings
        for fragment in ("canonical", "will not be re-emitted", "models z and rho only")))
    if not custom:
        # Familiar variable names do not prove familiar physics: a quartic
        # radial wall, changed epsilon, prefactor or CV expression must not be
        # overwritten merely because its left-hand side is named v_rad.
        from .lang.lexer import tokenize, NUMBER, EOF_
        def meaning(source):
            return [(t.kind, float(t.text) if t.kind == NUMBER else t.text)
                    for t in tokenize(source).tokens
                    if not t.is_trivia and t.kind != EOF_]
        custom = meaning(text) != meaning(emit_pot(spec))
    if custom:
        from .source_adapter import import_source
        import_source(spec, text, rep)
    return spec, rep


_KNOWN_LHS = {
    "fref", "fref_com", "fref_v", "fref_par", "fref_perp", "e1", "e2",
    "axis0", "p1", "p2", "e1r",
    "lig", "site", "core", "lig_com", "site_com", "core_com", "axis_raw",
    "axis_len", "axis", "origin", "d", "z", "perp", "rho", "z_cc", "r_cyl",
    "cone_slope", "r_allowed", "rad_excess", "k_rad", "v_rad", "z_min",
    "z_max", "k_z", "low_excess", "high_excess", "v_z", "ktemp", "h0",
    "sigma_z", "sigma_rho", "v_old", "hill", "v_meta", "v_total",
}


def _is_num(s: str) -> bool:
    return re.fullmatch(_NUM, s.strip()) is not None


def _set_num(spec: FunnelSpec, attr: str, literal: str) -> None:
    literal = literal.strip()
    try:
        value = float(literal)
    except ValueError:
        return
    setattr(spec, attr, value)
    spec.literals[attr] = literal
    spec.literal_values[attr] = value


def _label_for(base: str, text: str) -> str:
    """Recover the printed label of a diagnostic, e.g. tyr114_oh -> Tyr114_OH."""
    m = re.search(rf"print\s*\(\s*\"([^\"]*)_to_ligCOM_A\"\s*,\s*d_?{re.escape(base.split('_')[0])}",
                  text)
    if m:
        return m.group(1)
    parts = base.split("_")
    out = []
    for p in parts:
        out.append(p.upper() if len(p) <= 2 else p.capitalize())
    return "_".join(out)


def _gamma_from_comment(text: str) -> tuple[float | None, float | None]:
    m = re.search(r"gamma\s*=\s*([\d.]+)\s*at\s*([\d.]+)\s*K", text, re.I)
    if m:
        return float(m.group(1)), float(m.group(2))
    g = re.search(r"gamma\s*=\s*([\d.]+)", text, re.I)
    t = re.search(r"([\d.]+)\s*K\b", text)
    return (float(g.group(1)) if g else None,
            float(t.group(1)) if t else None)


# --------------------------------------------------------------------------
# emitting
# --------------------------------------------------------------------------
def _note(notes: dict, key: str, default: list[str]) -> list[str]:
    v = notes.get(key)
    return list(v) if v else list(default)


def _lig_comment_line(spec: FunnelSpec, structure) -> str | None:
    if structure is None or not spec.lig.indices:
        return None
    idx0 = spec.lig.idx0()
    idx0 = idx0[(idx0 >= 0) & (idx0 < structure.n_atoms)]
    if idx0.size == 0:
        return None
    chains = sorted({str(structure.chain[i]) for i in idx0})
    resn = sorted({str(structure.resname[i]).strip() for i in idx0})
    heavy = bool((structure.anum[idx0] > 1).all())
    chain_txt = "chain-" + "/".join(c if c else "-" for c in chains)
    res_txt = "/".join(resn)
    what = "heavy atoms only" if heavy else "atoms"
    return (f"# Target ligand: {spec.ligand_label} {chain_txt} {res_txt} "
            f"{what} ({idx0.size} atoms).")


def _sig_line(tag: str, spec: FunnelSpec, sel: Selection, structure) -> str | None:
    if structure is None or not sel.indices:
        return None
    names = structure.residue_signature(sel.idx0())
    if not names:
        return None
    return f"# {tag}: " + ", ".join(names) + "."


def _refresh_comments(spec: FunnelSpec, structure) -> dict[str, list[str]]:
    """Regenerate the data-bearing comment lines, keep the prose."""
    notes = {k: list(v) for k, v in spec.notes.items()}
    if not spec.auto_comments:
        return notes

    def replace_prefix(key: str, prefix: str, new: str | None):
        if new is None:
            return
        lines = notes.get(key, [])
        for i, ln in enumerate(lines):
            if ln.startswith(prefix):
                lines[i] = new
                notes[key] = lines
                return
        notes[key] = lines + [new]

    replace_prefix("lig", "# Target ligand:", _lig_comment_line(spec, structure))
    replace_prefix("frame", "# SITE:", _sig_line("SITE", spec, spec.site, structure))
    replace_prefix("frame", "# CORE:", _sig_line("CORE", spec, spec.core, structure))
    lit = Literals(spec)
    replace_prefix("wt", "# Well-tempered bias:",
                   f"# Well-tempered bias: gamma={fmt_g(spec.gamma)} at "
                   f"{fmt_g(spec.temperature)} K.")
    replace_prefix("wt", "# kTemp=",
                   f"# kTemp=(gamma-1)*kB*T={lit('ktemp', spec.ktemp)} kcal/mol.")
    return notes


def emit_pot(spec: FunnelSpec, structure=None) -> str:
    """Render the canonical funnel WT-MetaD potential file."""
    if spec.source_adapter:
        from .source_adapter import emit_source
        return emit_source(spec)
    lit = Literals(spec)
    notes = _refresh_comments(spec, structure)
    L: list[str] = []
    dim_word = {1: "one-dimensional", 2: "two-dimensional"}.get(spec.dimension,
                                                                f"{spec.dimension}-dimensional")

    # ---- header
    header = list(spec.header_comment)
    if not header:
        header = [
            f"# {spec.ligand_label} bound-start, {dim_word} well-tempered "
            "Funnel-MetaD potential.",
            "# Desmond M-expression; distances are in A and energies in "
            "kcal/mol.",
            "# The MetaD CV is the axial coordinate z.  rho defines the "
            "physical funnel wall.",
        ]
    elif spec.auto_comments:
        header = [re.sub(r"\b(one|two|three)-dimensional\b", dim_word, h)
                  for h in header]
    L += header
    L.append("")

    # ---- declare_meta
    L.append("declare_meta(")
    L.append(f"  dimension = {int(spec.dimension)},")
    L.append(f"  cutoff = {lit('kernel_cutoff', spec.kernel_cutoff)},")
    L.append(f"  first = {lit('meta_first', spec.meta_first)},")
    L.append(f"  interval = {lit('meta_interval', spec.meta_interval)},")
    L.append(f'  name = "{spec.kerseq_name}",')
    L.append(f'  initial = "{spec.initial_kernels}");')
    L.append("")
    if "output" in notes:
        L += notes["output"]
    L.append("declare_output(")
    L.append(f'  name = "{spec.cvseq_name}",')
    L.append(f"  first = {lit('cv_first', spec.cv_first)},")
    L.append(f"  interval = {lit('cv_interval', spec.cv_interval)});")
    L.append("")

    # ---- selections
    L += _note(notes, "lig", [])
    L.append(f'lig = atomsel("{spec.lig.asl()}");')
    L.append("")
    L += _note(notes, "frame", [])
    L.append(f'site = atomsel("{spec.site.asl()}");')
    L.append(f'core = atomsel("{spec.core.asl()}");')
    L.append("")
    L += _note(notes, "com", [])
    L.append("lig_com = center_of_mass(lig);")
    L.append("site_com = center_of_mass(site);")
    L.append("core_com = center_of_mass(core);")
    L.append("")

    # ---- axis
    L += _note(notes, "axis",
               ["# Translation- and rotation-invariant funnel axis."])
    L.append("axis_raw = min_image(site_com-core_com);")
    L.append("axis_len = norm(axis_raw);")
    if not spec.axis_is_tilted:
        L.append("axis = axis_raw/axis_len;")
    L.append("")

    # ---- body-fixed perpendicular frame, only when something needs it
    if spec.uses_perp_frame():
        from .terms import tilt_coefficients, linear_point
        L += _note(notes, "perpframe",
                   ["# Body-fixed perpendicular frame, so shapes placed off "
                    "the axis rotate",
                    "# and translate with the protein instead of with the "
                    "laboratory box."])
        L.append(f'{spec.frame_ref.var} = atomsel("{spec.frame_ref.asl()}");')
        L.append("fref_com = center_of_mass(fref);")
        L.append("fref_v = min_image(fref_com-core_com);")
        if spec.axis_is_tilted:
            L.append("axis0 = axis_raw/axis_len;")
            L.append("fref_par = dot(fref_v,axis0);")
            L.append("fref_perp = fref_v-fref_par*axis0;")
            L.append("p1 = fref_perp/norm(fref_perp);")
            L.append("p2 = cross(axis0,p1);")
            ca, cb, cc = tilt_coefficients(spec.axis_tilt_deg,
                                           spec.axis_azimuth_deg)
            # the angles themselves, so a round trip cannot drift in the last
            # digit of the three constants below
            L.append("# @ff-axis " + json.dumps(
                {"tilt_deg": spec.axis_tilt_deg,
                 "azimuth_deg": spec.axis_azimuth_deg},
                separators=(",", ":"), sort_keys=True))
            L.append("# The biased axis is tilted away from CORE->SITE by "
                     f"{spec.axis_tilt_deg:g} deg")
            L.append(f"# at azimuth {spec.axis_azimuth_deg:g} deg, in that "
                     "same body-fixed frame.")
            L.append("axis = " + linear_point(f"{fmt_number(ca)}*axis0",
                                              [(cb, "p1"), (cc, "p2")]) + ";")
            L.append("e1r = p1-dot(p1,axis)*axis;")
            L.append("e1 = e1r/norm(e1r);")
        else:
            L.append("fref_par = dot(fref_v,axis);")
            L.append("fref_perp = fref_v-fref_par*axis;")
            L.append("e1 = fref_perp/norm(fref_perp);")
        L.append("e2 = cross(axis,e1);")
        L.append("")

    # ---- origin + CVs
    L += _note(notes, "origin", [])
    if spec.origin_mode == "absolute":
        base = f"core_com+{lit('origin_offset', spec.origin_offset)}*axis"
    else:
        base = f"core_com+{lit('origin_frac', spec.origin_frac)}*axis_raw"
    from .terms import linear_point
    L.append("origin = " + linear_point(base, [(spec.origin_lat1, "e1"),
                                               (spec.origin_lat2, "e2")])
             + ";")
    L.append("d = min_image(lig_com-origin);")
    L.append("z = dot(d,axis);")
    L.append("perp = d-z*axis;")
    L.append(f"rho = sqrt(norm2(perp)+{EPS_LITERAL});")
    L.append("")

    # ---- funnel shape
    if spec.funnel_enabled:
        L += _note(notes, "funnel",
                   ["# Funnel: broad cone at the pocket, narrow cylinder in "
                    "bulk solvent."])
        L.append(f"z_cc = {lit('z_cc', spec.z_cc)};")
        L.append(f"r_cyl = {lit('r_cyl', spec.r_cyl)};")
        L.append(f"cone_slope = {lit('cone_slope', spec.cone_slope)};")
        L.append("r_allowed = if z_cc-z then r_cyl+(z_cc-z)*cone_slope "
                 "else r_cyl;")
        L.append("")

        # ---- radial wall
        L += _note(notes, "rad",
                   ["# One-sided, flat-bottom radial wall: zero when rho <= "
                    "r_allowed."])
        L.append("rad_excess = if rho-r_allowed then rho-r_allowed else 0.0;")
        L.append(f"k_rad = {lit('k_rad', spec.k_rad)};")
        L.append("v_rad = 0.5*k_rad*rad_excess^2;")
        L.append("")

        # ---- axial walls
        L += _note(notes, "zwall",
                   ["# Axial end walls, restricted to the supplied solvent "
                    "box."])
        L.append(f"z_min = {lit('z_min', spec.z_min)};")
        L.append(f"z_max = {lit('z_max', spec.z_max)};")
        L.append(f"k_z = {lit('k_z', spec.k_z)};")
        L.append("low_excess = if z_min-z then z_min-z else 0.0;")
        L.append("high_excess = if z-z_max then z-z_max else 0.0;")
        L.append("v_z = 0.5*k_z*(low_excess^2+high_excess^2);")
        L.append("")
    else:
        L.append("# The built-in funnel is switched off; the shape terms below")
        L.append("# are the whole restraint.")
        L.append("")

    # ---- extra shape terms
    if spec.terms:
        L += _note(notes, "terms",
                   ["# Extra potential terms.  Each block is self-contained "
                    "and adds",
                    "# its own v_<name> to the total.  The @ff-term line is a "
                    "machine-",
                    "# readable copy of the term for the editor; Desmond "
                    "ignores it."])
        ctx = EmitCtx(funnel_enabled=spec.funnel_enabled)
        for t in spec.terms:
            if t.enabled:
                L += emit_term(t, ctx)
            else:
                L.append(t.marker())
                L.append(f"# {t.label or t.var}: disabled, not part of the "
                         "potential.")
            L.append("")

    # ---- well tempered metadynamics
    L += _note(notes, "wt", [])
    L.append(f"ktemp = {lit('ktemp', spec.ktemp)};")
    L.append(f"h0 = {lit('h0', spec.h0)};")
    L.append(f"sigma_z = {lit('sigma_z', spec.sigma_z)};")
    if spec.dimension >= 2:
        L.append(f"sigma_rho = {lit('sigma_rho', spec.sigma_rho)};")
    zeros = ",".join(["0.0"] * (spec.dimension + 1))
    cvs = "z" if spec.dimension == 1 else "z,rho"
    sig = "sigma_z" if spec.dimension == 1 else "sigma_z,sigma_rho"
    L.append(f"v_old = meta(0,array({zeros}),array({cvs}));")
    L.append("hill = h0*exp(v_old/(-ktemp));")
    L.append(f"v_meta = meta(0,array(hill,{sig}),array({cvs}));")
    L.append("")

    # ---- diagnostics
    if spec.diagnostics:
        L += _note(notes, "diag",
                   ["# Diagnostics are recorded but are not biased CVs."])
        for d in spec.diagnostics:
            L.append(f'{d.var}_sel = atomsel("atom. '
                     f'{",".join(str(i) for i in d.indices)}");')
        for d in spec.diagnostics:
            L.append(f"{d.var} = center_of_mass({d.var}_sel);")
        for d in spec.diagnostics:
            L.append(f"{d.dist_var} = norm(min_image(lig_com-{d.var}));")
        L.append("")

    # ---- total
    L += _note(notes, "total", [])
    parts = ["v_rad", "v_z"] if spec.funnel_enabled else []
    parts.append("v_meta")
    parts += [t.energy_var for t in spec.terms if t.enabled]
    L.append("v_total = " + "+".join(parts) + ";")
    L.append("")

    # ---- prints
    L += _note(notes, "print", [])
    allowed = set(spec.available_print_vars())
    printed_vars = set()
    for label, var in spec.prints:
        if var not in allowed:
            continue
        L.append(f'print("{label}",{var});')
        printed_vars.add(var)
    # A reported distance exists only to be reported.  Whoever created the
    # diagnostic - the panel, a script, a session file - it is printed here
    # if nothing already prints it, so the value cannot be computed into the
    # file and then silently never written to the CV output.
    for d in spec.diagnostics:
        if d.dist_var in printed_vars or d.dist_var not in allowed:
            continue
        L.append(f'print("{d.print_label}",{d.dist_var});')
        printed_vars.add(d.dist_var)
    L.append("")
    L.append("v_total;")
    if "footer" in notes:
        L += notes["footer"]
    return "\n".join(L) + "\n"


def default_spec_for(structure, lig_idx: list[int], site_idx: list[int],
                     core_idx: list[int], label: str = "LIG") -> FunnelSpec:
    """A sane starting funnel for a freshly picked set of groups."""
    spec = FunnelSpec()
    spec.lig = Selection("lig", sorted(lig_idx))
    spec.site = Selection("site", sorted(site_idx))
    spec.core = Selection("core", sorted(core_idx))
    spec.ligand_label = label
    spec.prints = list(DEFAULT_PRINTS)
    spec.ktemp = spec.ktemp_from_gamma()
    return spec
