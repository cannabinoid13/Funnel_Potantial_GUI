"""Composable potential terms: any shape, anywhere, in any number.

A job's restraint is a sum of terms.  Each term is one of

``lathe``
    a surface of revolution: you give a profile ``r(z)`` as an expression and
    it is turned on a lathe around the funnel axis.  The volume's ends are
    closed automatically - by default only the solvent-facing end, which is
    the one a dissociating ligand would otherwise escape through.
``sphere``
    a spherical well or exclusion, centred on a point of the funnel axis or on
    the centre of mass of an atom group.
``slab``
    an axial window: two one-sided walls at ``z_lo`` and ``z_hi``.
``expression``
    a complete Desmond M-expression that you write yourself; the energy is
    exactly what you type.

Every term can be confined to a region of the axis with a smooth (C1)
switching function, so different parts of the funnel can carry different
potentials - a tighter wall in the bulk cylinder, for instance, to cut down
solvent exploration.

Everything a term emits obeys the Desmond M-expression rules documented in the
user guide, chapter 11: single assignment, ``^`` only with integer exponents,
``if c then a else b`` with the positive branch taken when ``c > 0``, and only
functions that actually exist in the language.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict

import numpy as np

EPS_LITERAL = "1.0e-12"

KINDS = ["lathe", "sphere", "slab", "expression"]
KIND_LABELS = {
    "lathe": "Lathe  ·  revolve a profile r(z)",
    "sphere": "Sphere  ·  distance from a point or group",
    "slab": "Slab  ·  axial window",
    "expression": "Expression  ·  write the energy yourself",
}
MODES = ["confine", "exclude"]
CAPS = ["solvent", "pocket", "both", "none"]

#: Ready-made profiles for the lathe, as (label, expression, note).
PROFILE_PRESETS = [
    ("Cylinder", "6.0", "constant radius"),
    ("Cone opening to the pocket", "4.0+(26.0-z)*0.6", "linear taper"),
    ("Hourglass neck", "4.0+0.06*(z-15.0)^2", "quadratic waist at z = 15"),
    ("Trumpet", "3.0+0.004*(34.0-z)^3", "cubic flare"),
    ("Gaussian bulb", "4.0+8.0*exp(-(z-10.0)^2/50.0)", "bulge at z = 10"),
    ("Sigmoid step", "4.0+6.0/(1.0+exp((z-20.0)/2.0))", "wide then narrow"),
    ("Sphere-like barrel", "sqrt(pow(100.0-(z-15.0)^2,0.5))",
     "circular arc, guarded by pow"),
]


# --------------------------------------------------------------------------
@dataclass
class Region:
    """Optional axial window with a smooth switch."""

    enabled: bool = False
    z_lo: float = 0.0
    z_hi: float = 30.0
    taper: float = 2.0            # A; 0 makes the switch a hard gate

    def copy(self) -> "Region":
        return Region(self.enabled, self.z_lo, self.z_hi, self.taper)

    def switch(self, z) -> np.ndarray:
        z = np.asarray(z, dtype=np.float64)
        if not self.enabled:
            return np.ones_like(z)
        if self.taper <= 0:
            return np.where((z > self.z_lo) & (z < self.z_hi), 1.0, 0.0)
        a = np.clip((z - self.z_lo) / self.taper, 0.0, 1.0)
        b = np.clip((self.z_hi - z) / self.taper, 0.0, 1.0)
        return (a * a * (3.0 - 2.0 * a)) * (b * b * (3.0 - 2.0 * b))


@dataclass
class Term:
    var: str = "w1"
    label: str = "term"
    kind: str = "lathe"
    enabled: bool = True
    color: tuple = (0.45, 0.85, 0.95)

    # what the term acts on
    target_kind: str = "ligand"          # 'ligand' | 'selection'
    target_indices: list = field(default_factory=list)
    target_expression: str = ""

    # wall strength
    mode: str = "confine"                # push in / push out
    k: float = 25.0
    exponent: int = 2
    offset: float = 0.0                  # slack before the wall engages

    # lathe
    profile: str = "6.0"
    z_lo: float = -4.0
    z_hi: float = 34.0
    cap: str = "solvent"
    k_cap: float = 50.0
    #: hard-cap the turned radius so the wall can never sit outside the
    #: solvent box; the cap is written into the potential itself
    clamp_radius: bool = False
    radius_cap: float = 0.0

    # placement: axial position is z_lo/z_hi (lathe, slab) or center_z
    # (sphere); these two slide the shape sideways in the body-fixed frame.
    offset_e1: float = 0.0
    offset_e2: float = 0.0
    #: axial offset of the shape's own origin, needed once it is tilted
    offset_axial: float = 0.0
    #: orientation of the shape's own axis: tilt away from the funnel axis
    #: and the azimuth of that tilt in the body-fixed (e1, e2) plane
    tilt_deg: float = 0.0
    azimuth_deg: float = 0.0

    # sphere
    center_mode: str = "axis"            # 'axis' | 'group'
    center_z: float = 0.0
    center_indices: list = field(default_factory=list)
    center_expression: str = ""
    radius: float = 10.0

    # expression
    expression: str = "0.0"

    # how the potential's own gradation is drawn for this term
    own_levels: bool = False
    level_mode: str = "distance"          # 'distance' (A) or 'energy'
    levels: list = field(default_factory=lambda: [0.75, 1.5, 2.25])

    region: Region = field(default_factory=Region)

    # ------------------------------------------------------------------
    def copy(self) -> "Term":
        t = Term(**{k: v for k, v in self.__dict__.items()
                    if k not in ("region", "target_indices",
                                 "center_indices", "color", "levels")})
        t.region = self.region.copy()
        t.target_indices = list(self.target_indices)
        t.center_indices = list(self.center_indices)
        t.color = tuple(self.color)
        t.levels = list(self.levels)
        return t

    def to_dict(self, compact: bool = False) -> dict:
        d = asdict(self)
        d["color"] = [round(float(c), 4) for c in self.color]
        if not compact:
            return d
        ref = asdict(Term())
        ref["color"] = [round(float(c), 4) for c in Term().color]
        out = {"var": d["var"], "kind": d["kind"]}
        for k, v in d.items():
            if k in ("var", "kind"):
                continue
            if k == "region":
                if v != ref["region"]:
                    out["region"] = {rk: rv for rk, rv in v.items()
                                     if rv != ref["region"][rk]}
                continue
            if v != ref[k]:
                out[k] = v
        return out

    @staticmethod
    def from_dict(d: dict) -> "Term":
        d = dict(d)
        reg = d.pop("region", {}) or {}
        t = Term()
        for k, v in d.items():
            if hasattr(t, k):
                setattr(t, k, v)
        t.color = tuple(t.color)
        t.region = Region(**{k: reg[k] for k in
                             ("enabled", "z_lo", "z_hi", "taper")
                             if k in reg})
        return t

    @property
    def energy_var(self) -> str:
        return f"v_{self.var}"

    @property
    def is_expression(self) -> bool:
        return self.kind == "expression"

    @property
    def is_off_axis(self) -> bool:
        return bool(self.offset_e1 or self.offset_e2 or self.offset_axial)

    @property
    def is_tilted(self) -> bool:
        return abs(float(self.tilt_deg)) > 1e-9

    @property
    def needs_perp_frame(self) -> bool:
        return bool(self.offset_e1 or self.offset_e2 or self.is_tilted)

    def axis_coefficients(self) -> tuple:
        """(along axis, along e1, along e2) of this shape's own unit axis."""
        return tilt_coefficients(self.tilt_deg, self.azimuth_deg)

    def tilt_components(self) -> tuple:
        """The tilt as two independent angles, one per perpendicular."""
        t = math.radians(self.tilt_deg)
        a = math.radians(self.azimuth_deg)
        return (math.degrees(t * math.cos(a)), math.degrees(t * math.sin(a)))

    def set_tilt_components(self, a1_deg: float, a2_deg: float) -> None:
        a1 = math.radians(a1_deg)
        a2 = math.radians(a2_deg)
        self.tilt_deg = math.degrees(math.hypot(a1, a2))
        self.azimuth_deg = math.degrees(math.atan2(a2, a1)) if (a1 or a2) \
            else 0.0

    def lever(self) -> float:
        """Half length of the shape, the arm an end handle turns it by."""
        if self.kind == "sphere":
            return max(1.0, float(self.radius))
        return max(1.0, 0.5 * abs(float(self.z_hi) - float(self.z_lo)))

    def axial_position(self) -> float:
        """Where the shape sits along the axis, for the readouts."""
        if self.kind == "sphere":
            return self.center_z
        return 0.5 * (self.z_lo + self.z_hi)

    def move_axially(self, new_center: float) -> None:
        """Slide the shape along the axis, keeping its size."""
        if self.kind == "sphere":
            self.center_z = new_center
            return
        half = 0.5 * (self.z_hi - self.z_lo)
        self.z_lo = new_center - half
        self.z_hi = new_center + half

    def marker(self) -> str:
        """A machine-readable record of the term, ignored by Desmond.

        The M-expression below it is what Desmond runs; this line is how the
        interface reads the term back without having to reverse-engineer the
        generated code.  On import the block is regenerated from the marker
        and compared with the file, so the two can never drift apart silently.
        """
        payload = json.dumps(self.to_dict(compact=True),
                             separators=(",", ":"), sort_keys=True)
        return f"# @ff-term {payload}"

    # ------------------------------------------------------------------
    # numeric evaluation (independent of the emitted text where possible)
    # ------------------------------------------------------------------
    def _excess(self, value, limit) -> np.ndarray:
        """One-sided distance past the boundary, with the slack applied."""
        value = np.asarray(value, dtype=np.float64)
        limit = np.asarray(limit, dtype=np.float64)
        if self.mode == "exclude":
            e = limit + self.offset - value
        else:
            e = value - limit - self.offset
        return np.where(e > 0.0, e, 0.0)

    def energy(self, ctx, apply_region: bool = True) -> np.ndarray:
        """Energy of this term for the sample in ``ctx`` (a :class:`FieldCtx`).

        ``apply_region=False`` gives the bare shape without the switching
        function, which is what the 3-D view draws as the term's boundary: a
        switched wall is enormous just outside a narrow taper, so an absolute
        iso-level of the switched field degenerates into paper-thin sheets at
        the region edges instead of showing the shape.
        """
        if not self.enabled:
            return np.zeros_like(ctx.z)
        tz, trho = ctx.term_cv(self)
        # The region is read on the **funnel** z, never on the shape's own
        # tilted coordinate: a region means "while the run is in this stretch
        # of the funnel", which is also exactly what the emitted potential
        # computes (`w<n>_s` is built from `z`).  Keeping the two in step
        # matters - when they drifted apart, a tilted shape was silenced in
        # the model while the file still applied it.
        sw = (self.region.switch(ctx.z) if apply_region
              else np.ones_like(ctx.z))
        if self.kind == "slab":
            lo = np.where(self.z_lo - tz > 0.0, self.z_lo - tz, 0.0)
            hi = np.where(tz - self.z_hi > 0.0, tz - self.z_hi, 0.0)
            v = 0.5 * self.k * (lo ** self.exponent + hi ** self.exponent)
            return v * sw
        if self.kind == "sphere":
            d = ctx.distance_to_center(self)
            e = self._excess(d, self.radius)
            return 0.5 * self.k * e ** self.exponent * sw
        if self.kind == "lathe":
            r = ctx.profile_radius(self)
            r = np.where(r > 0.0, r, 0.0)
            if self.clamp_radius and self.radius_cap > 0:
                r = np.minimum(r, self.radius_cap)
            e = self._excess(trho, r)
            v = 0.5 * self.k * e ** self.exponent
            if self.cap in ("solvent", "both"):
                hi = np.where(tz - self.z_hi > 0.0, tz - self.z_hi, 0.0)
                v = v + 0.5 * self.k_cap * hi ** 2
            if self.cap in ("pocket", "both"):
                lo = np.where(self.z_lo - tz > 0.0, self.z_lo - tz, 0.0)
                v = v + 0.5 * self.k_cap * lo ** 2
            return v * sw
        if self.kind == "expression":
            v = ctx.expression_value(self)
            return v * sw
        return np.zeros_like(ctx.z)

    # ------------------------------------------------------------------
    # geometry helpers used by the 3-D view and the plots
    # ------------------------------------------------------------------
    def boundary_radius(self, ctx, z) -> np.ndarray | None:
        """r(z) of the term's boundary, or None if it has no lathe profile."""
        if self.kind == "lathe":
            r = ctx.profile_radius(self, z=z)
            r = np.where(r > 0.0, r, 0.0)
            if self.clamp_radius and self.radius_cap > 0:
                r = np.minimum(r, self.radius_cap)
            return r
        if self.kind == "sphere" and self.center_mode == "axis":
            dz = np.asarray(z, dtype=np.float64) - self.center_z
            inside = self.radius ** 2 - dz ** 2
            return np.where(inside > 0.0, np.sqrt(np.maximum(inside, 0.0)),
                            np.nan)
        return None

    def radial_offset_for(self, v: float) -> float:
        if self.k <= 0:
            return float("inf")
        if self.exponent == 2:
            return math.sqrt(max(0.0, 2.0 * v / self.k))
        return (max(0.0, 2.0 * v / self.k)) ** (1.0 / self.exponent)

    def energy_at_distance(self, d: float) -> float:
        return 0.5 * self.k * max(0.0, d) ** self.exponent

    def gradation(self, distances=(0.5, 1.0, 2.0, 3.0)) -> list:
        """(distance, energy) pairs showing how fast this wall climbs."""
        return [(float(d), self.energy_at_distance(float(d)))
                for d in distances]


# --------------------------------------------------------------------------
# evaluation context
# --------------------------------------------------------------------------
class FieldCtx:
    """Everything the terms need to evaluate on a set of sample points.

    The context always keeps real 3-D positions.  A shape placed off the axis
    breaks the axial symmetry, so evaluating from ``(z, rho)`` alone would be
    ambiguous; when only CVs are supplied the points are reconstructed in the
    e1 plane (azimuth 0), which is exactly the plane the cross-section and the
    2-D maps draw.
    """

    def __init__(self, model, points=None, z=None, rho=None):
        self.model = model
        self.st = model.st
        self.spec = model.spec
        fr = model.frame
        self.frame = fr
        if points is not None:
            pts = np.asarray(points, dtype=np.float64)
            self.points = pts.reshape(-1, 3) if pts.ndim > 1 else pts[None, :]
            zz, rr = model.cv(self.points)
            self.z = np.asarray(zz, dtype=np.float64)
            self.rho = np.asarray(rr, dtype=np.float64)
        else:
            self.z = np.atleast_1d(np.asarray(z, dtype=np.float64))
            self.rho = np.atleast_1d(np.asarray(rho, dtype=np.float64))
            self.points = fr.cyl_to_world(self.z, self.rho,
                                          np.zeros_like(self.z))
        self._cache: dict = {}

    # -- helpers
    def _target_fixed(self, term: Term):
        """(z, rho, position) of a term's own group, or None for the ligand."""
        if term.target_kind == "ligand" or not term.target_indices:
            return None
        key = ("tgt", tuple(term.target_indices))
        if key not in self._cache:
            com = self.st.center_of_mass(np.asarray(term.target_indices) - 1)
            z, rho = self.model.cv(com)
            self._cache[key] = (float(z), float(rho), com)
        return self._cache[key]

    def target_points(self, term: Term) -> np.ndarray:
        """Positions the term acts on: the samples, or its own fixed group."""
        fixed = self._target_fixed(term)
        if fixed is None:
            return self.points
        _z, _rho, pos = fixed
        return np.broadcast_to(np.asarray(pos, dtype=np.float64),
                              self.points.shape)

    def shape_origin(self, term: Term) -> np.ndarray:
        """The shape's own axis origin, including its placement offsets."""
        fr = self.frame
        return (fr.origin + term.offset_axial * fr.axis
                + term.offset_e1 * fr.e1 + term.offset_e2 * fr.e2)

    def shape_axis(self, term: Term) -> np.ndarray:
        """The shape's own unit axis, tilted away from the funnel axis."""
        fr = self.frame
        if not term.is_tilted:
            return fr.axis
        return unit_from_tilt(fr.axis, fr.e1, fr.e2, term.tilt_deg,
                              term.azimuth_deg)

    def term_cv(self, term: Term):
        """(z, rho) of the target measured about the shape's own axis."""
        pts = self.target_points(term)
        if not (term.is_off_axis or term.is_tilted):
            fixed = self._target_fixed(term)
            if fixed is None:
                return self.z, self.rho
            z, rho, _ = fixed
            return np.full_like(self.z, z), np.full_like(self.rho, rho)
        u = self.shape_axis(term)
        d = self.st.min_image(pts - self.shape_origin(term))
        z = d @ u
        perp = d - z[..., None] * u
        rho = np.sqrt(np.einsum("ij,ij->i", perp, perp) + 1.0e-12)
        return z, rho

    # kept for compatibility with the earlier name
    def term_z_rho(self, term: Term):
        return self.term_cv(term)

    def distance_to_center(self, term: Term) -> np.ndarray:
        fr = self.frame
        if term.center_mode == "group" and term.center_indices:
            key = ("ctr", tuple(term.center_indices))
            if key not in self._cache:
                self._cache[key] = self.st.center_of_mass(
                    np.asarray(term.center_indices) - 1)
            centre = np.asarray(self._cache[key], dtype=np.float64)
            centre = (centre + term.offset_axial * fr.axis
                      + term.offset_e1 * fr.e1 + term.offset_e2 * fr.e2)
        else:
            centre = (self.shape_origin(term)
                      + term.center_z * self.shape_axis(term))
        pts = self.target_points(term)
        return np.linalg.norm(self.st.min_image(pts - centre), axis=-1)

    def profile_radius(self, term: Term, z=None) -> np.ndarray:
        """Evaluate a lathe profile expression on the sample's z values."""
        if z is None:
            zz, _rr = self.term_cv(term)
        else:
            zz = np.asarray(z, dtype=np.float64)
        from .mexpr import Interpreter, MExprError
        env = self.base_env(zz, np.zeros_like(zz))
        try:
            interp = Interpreter(self.st, env=env)
            v = interp.eval_expression(term.profile, env)
            arr = np.asarray(v.data)
            return np.broadcast_to(arr[..., 0], zz.shape).astype(np.float64)
        except MExprError:
            return np.full_like(zz, np.nan)

    def expression_value(self, term: Term) -> np.ndarray:
        from .mexpr import Interpreter, MExprError
        zz, rr = self.term_cv(term)
        env = self.base_env(zz, rr)
        try:
            interp = Interpreter(self.st, env=env)
            v = interp.eval_expression(term.expression, env)
            arr = np.asarray(v.data)
            if arr.shape[-1] != 1:
                raise MExprError("the expression must evaluate to a single "
                                 f"number, got an array of length "
                                 f"{arr.shape[-1]}")
            return np.broadcast_to(arr[..., 0], zz.shape).astype(np.float64)
        except MExprError:
            return np.full_like(zz, np.nan)

    def base_env(self, z=None, rho=None) -> dict:
        """The variables an expression may use, as interpreter values."""
        from .mexpr import V, NUM
        z = self.z if z is None else z
        rho = self.rho if rho is None else rho
        sp = self.spec
        fr = self.frame
        env = {
            "z": V(NUM, np.asarray(z, dtype=np.float64)[..., None]),
            "rho": V(NUM, np.asarray(rho, dtype=np.float64)[..., None]),
            "axis_len": V.num(fr.axis_len),
            "axis": V(NUM, fr.axis),
            "origin": V(NUM, fr.origin),
            "lig_com": V(NUM, fr.lig_com),
            "site_com": V(NUM, fr.site_com),
            "core_com": V(NUM, fr.core_com),
        }
        if sp.has_frame_ref:
            env["e1"] = V(NUM, fr.e1)
            env["e2"] = V(NUM, fr.e2)
        if sp.funnel_enabled:
            env["r_allowed"] = V(
                NUM, np.asarray(sp.r_allowed(z), dtype=np.float64)[..., None])
            env["z_cc"] = V.num(sp.z_cc)
            env["r_cyl"] = V.num(sp.r_cyl)
            env["cone_slope"] = V.num(sp.cone_slope)
            env["z_min"] = V.num(sp.z_min)
            env["z_max"] = V.num(sp.z_max)
            env["k_rad"] = V.num(sp.k_rad)
            env["k_z"] = V.num(sp.k_z)
        env["ktemp"] = V.num(sp.ktemp)
        env["h0"] = V.num(sp.h0)
        env["sigma_z"] = V.num(sp.sigma_z)
        return env


def expression_variables(spec) -> list[tuple[str, str]]:
    """(name, description) of everything an expression term may reference."""
    out = [
        ("z", "axial CV of the target, Å (0 at the funnel origin)"),
        ("rho", "radial CV of the target, Å"),
        ("axis_len", "length of the CORE→SITE vector, Å"),
        ("d", "target COM minus origin, a 3-vector"),
        ("axis", "unit vector along the funnel axis"),
        ("origin", "funnel origin, a 3-vector"),
        ("lig_com", "ligand centre of mass"),
        ("site_com", "SITE centre of mass"),
        ("core_com", "CORE centre of mass"),
        ("ktemp", "well-tempered kTemp, kcal/mol"),
        ("h0", "hill height, kcal/mol"),
        ("sigma_z", "kernel width, Å"),
    ]
    if getattr(spec, "funnel_enabled", True):
        out[3:3] = [
            ("r_allowed", "funnel radius at the target's z, Å"),
            ("z_cc", "cone/cylinder junction, Å"),
            ("r_cyl", "cylinder radius, Å"),
            ("cone_slope", "cone dr/dz"),
            ("z_min", "lower axial wall, Å"),
            ("z_max", "upper axial wall, Å"),
            ("k_rad", "radial force constant"),
            ("k_z", "axial force constant"),
        ]
    return out


# --------------------------------------------------------------------------
# emission
# --------------------------------------------------------------------------
def tilt_coefficients(tilt_deg: float, azimuth_deg: float) -> tuple:
    """Unit vector components in a body-fixed (axis, e1, e2) frame.

    Written into the potential as three constants, so the direction rotates
    and translates with the protein without any run-time trigonometry.
    """
    t = math.radians(float(tilt_deg))
    a = math.radians(float(azimuth_deg))
    return (math.cos(t), math.sin(t) * math.cos(a), math.sin(t) * math.sin(a))


def unit_from_tilt(axis, e1, e2, tilt_deg: float, azimuth_deg: float):
    ca, cb, cc = tilt_coefficients(tilt_deg, azimuth_deg)
    v = ca * np.asarray(axis) + cb * np.asarray(e1) + cc * np.asarray(e2)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else np.asarray(axis)


def fmt(v: float) -> str:
    """Number formatting shared with the .pot writer."""
    from .potfile import fmt_number
    return fmt_number(v)


def sub(var: str, value: float) -> str:
    """``var - value`` written so a negative value never gives ``a--b``."""
    if value < 0:
        return f"{var}+{fmt(-value)}"
    return f"{var}-{fmt(value)}"


def rsub(value: float, var: str) -> str:
    """``value - var``, with the constant first."""
    return f"{fmt(value)}-{var}"


def linear_point(base: str, terms: list) -> str:
    """``base + a*u + b*v ...`` with signs folded in, zero terms dropped."""
    out = base
    for coeff, unit in terms:
        c = float(coeff)
        if c == 0.0:
            continue
        if c < 0:
            out += f"-{fmt(-c)}*{unit}"
        else:
            out += f"+{fmt(c)}*{unit}"
    return out


@dataclass
class EmitCtx:
    """Names available in the generated file when a term is emitted."""

    funnel_enabled: bool = True
    lig_z: str = "z"
    lig_rho: str = "rho"
    lig_com: str = "lig_com"


def emit_term(term: Term, ctx: EmitCtx) -> list[str]:
    """The M-expression lines for one term, ending with ``v_<var> = ...``."""
    v = term.var
    L: list[str] = [term.marker()]
    if term.label:
        L.append(f"# {term.label}: {_describe(term)}")

    own_target = (term.target_kind == "selection" and term.target_indices)
    placed = term.is_off_axis or term.is_tilted

    # -- the group this term acts on
    if own_target:
        idx = ",".join(str(i) for i in term.target_indices)
        L.append(f'{v}_grp = atomsel("atom. {idx}");')
        L.append(f"{v}_com = center_of_mass({v}_grp);")
        com_var = f"{v}_com"
    else:
        com_var = ctx.lig_com

    # -- the shape's own frame: origin slid in the body-fixed directions and
    #    an axis tilted away from the funnel axis.  Both are combinations of
    #    axis/e1/e2, so they rotate with the protein.
    if term.is_off_axis:
        L.append(f"{v}_ax = "
                 + linear_point("origin", [(term.offset_axial, "axis"),
                                           (term.offset_e1, "e1"),
                                           (term.offset_e2, "e2")]) + ";")
        base_var = f"{v}_ax"
    else:
        base_var = "origin"
    if term.is_tilted:
        ca, cb, cc = term.axis_coefficients()
        L.append(f"{v}_u = " + linear_point(f"{fmt(ca)}*axis",
                                            [(cb, "e1"), (cc, "e2")]) + ";")
        axis_var = f"{v}_u"
    else:
        axis_var = "axis"

    needs = _needs_cv(term)
    if own_target or placed:
        if needs:
            L.append(f"{v}_dv = min_image({com_var}-{base_var});")
            L.append(f"{v}_z = dot({v}_dv,{axis_var});")
            L.append(f"{v}_pp = {v}_dv-{v}_z*{axis_var};")
            L.append(f"{v}_rho = sqrt(norm2({v}_pp)+{EPS_LITERAL});")
        z_var, rho_var = f"{v}_z", f"{v}_rho"
    else:
        z_var, rho_var = ctx.lig_z, ctx.lig_rho

    # -- switching function, always read on the funnel axis
    switch_var = None
    if term.region.enabled:
        L += _emit_switch(term, ctx.lig_z)
        switch_var = f"{v}_s"

    body: list[str] = []
    exp = int(max(1, round(term.exponent)))

    if term.kind == "lathe":
        L.append(f"{v}_r0 = {_profile_with_z(term.profile, z_var)};")
        if term.clamp_radius and term.radius_cap > 0:
            L.append(f"{v}_r1 = if {v}_r0 then {v}_r0 else 0.0;")
            cap = fmt(term.radius_cap)
            L.append(f"{v}_r = if {cap}-{v}_r1 then {v}_r1 else {cap};")
        else:
            L.append(f"{v}_r = if {v}_r0 then {v}_r0 else 0.0;")
        L += _emit_excess(term, v, rho_var, f"{v}_r")
        L.append(f"{v}_wall = 0.5*{fmt(term.k)}*{v}_e^{exp};")
        parts = [f"{v}_wall"]
        caps: list[str] = []
        if term.cap in ("solvent", "both"):
            hi = sub(z_var, term.z_hi)
            L.append(f"{v}_hi = if {hi} then {hi} else 0.0;")
            caps.append(f"{v}_hi^2")
        if term.cap in ("pocket", "both"):
            lo = rsub(term.z_lo, z_var)
            L.append(f"{v}_lo = if {lo} then {lo} else 0.0;")
            caps.append(f"{v}_lo^2")
        if caps:
            L.append(f"{v}_cap = 0.5*{fmt(term.k_cap)}*({'+'.join(caps)});")
            parts.append(f"{v}_cap")
        body = ["+".join(parts)]

    elif term.kind == "sphere":
        if term.center_mode == "group" and term.center_indices:
            idx = ",".join(str(i) for i in term.center_indices)
            L.append(f'{v}_csel = atomsel("atom. {idx}");')
            L.append(f"{v}_c = "
                     + linear_point(f"center_of_mass({v}_csel)",
                                    [(term.offset_axial, "axis"),
                                     (term.offset_e1, "e1"),
                                     (term.offset_e2, "e2")]) + ";")
        elif term.center_z == 0:
            L.append(f"{v}_c = {base_var};")
        else:
            L.append(f"{v}_c = "
                     + linear_point(base_var,
                                    [(term.center_z, axis_var)]) + ";")
        L.append(f"{v}_dc = min_image({com_var}-{v}_c);")
        L.append(f"{v}_d = norm({v}_dc);")
        L += _emit_excess(term, v, f"{v}_d", fmt(term.radius))
        L.append(f"{v}_wall = 0.5*{fmt(term.k)}*{v}_e^{exp};")
        body = [f"{v}_wall"]

    elif term.kind == "slab":
        lo = rsub(term.z_lo, z_var)
        hi = sub(z_var, term.z_hi)
        L.append(f"{v}_lo = if {lo} then {lo} else 0.0;")
        L.append(f"{v}_hi = if {hi} then {hi} else 0.0;")
        L.append(f"{v}_wall = 0.5*{fmt(term.k)}*({v}_lo^{exp}+{v}_hi^{exp});")
        body = [f"{v}_wall"]

    elif term.kind == "expression":
        expr = _substitute_target(term.expression, z_var, rho_var, ctx)
        body = [expr]

    else:
        body = ["0.0"]

    total = body[0]
    if switch_var:
        if not _atomic(total):
            total = f"({total})"
        L.append(f"{term.energy_var} = {switch_var}*{total};")
    else:
        L.append(f"{term.energy_var} = {total};")
    return L


def _atomic(expr: str) -> bool:
    """True when the expression needs no brackets before being multiplied."""
    e = expr.strip()
    if e.isidentifier():
        return True
    if e.startswith("(") and e.endswith(")"):
        depth = 0
        for i, ch in enumerate(e):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i == len(e) - 1
    return False


def _needs_cv(term: Term) -> bool:
    """Does this term actually use its target's z and rho?"""
    if term.kind in ("lathe", "slab"):
        return True
    if term.region.enabled:
        return True
    if term.kind == "expression":
        return bool(_rename_var(term.expression, "z", "\x00") !=
                    term.expression
                    or _rename_var(term.expression, "rho", "\x00") !=
                    term.expression)
    return False


def _emit_excess(term: Term, v: str, value_var: str, limit: str) -> list[str]:
    """``<v>_e`` = how far past the boundary the target is, else zero."""
    off = term.offset
    if term.mode == "exclude":
        lhs = f"{limit}-{value_var}"
        if off:
            lhs = f"{limit}+{fmt(off)}-{value_var}"
    else:
        lhs = f"{value_var}-{limit}"
        if off:
            lhs = f"{value_var}-{limit}-{fmt(off)}"
    return [f"{v}_e = if {lhs} then {lhs} else 0.0;"]


def _emit_switch(term: Term, z_var: str) -> list[str]:
    v = term.var
    r = term.region
    if r.taper <= 0:
        return [f"{v}_s = if {sub(z_var, r.z_lo)} then "
                f"(if {rsub(r.z_hi, z_var)} then 1.0 else 0.0) else 0.0;"]
    return [
        f"{v}_sa = ({sub(z_var, r.z_lo)})/{fmt(r.taper)};",
        f"{v}_sac = if {v}_sa then (if 1.0-{v}_sa then {v}_sa else 1.0) "
        f"else 0.0;",
        f"{v}_sb = ({rsub(r.z_hi, z_var)})/{fmt(r.taper)};",
        f"{v}_sbc = if {v}_sb then (if 1.0-{v}_sb then {v}_sb else 1.0) "
        f"else 0.0;",
        f"{v}_s = {v}_sac^2*(3.0-2.0*{v}_sac)*{v}_sbc^2*(3.0-2.0*{v}_sbc);",
    ]


def _profile_with_z(profile: str, z_var: str) -> str:
    """Rewrite a profile written in terms of ``z`` for another target."""
    if z_var == "z":
        return profile
    return _rename_var(profile, "z", z_var)


def _substitute_target(expr: str, z_var: str, rho_var: str,
                       ctx: EmitCtx) -> str:
    out = expr
    if z_var != "z":
        out = _rename_var(out, "z", z_var)
    if rho_var != "rho":
        out = _rename_var(out, "rho", rho_var)
    return out


def _rename_var(text: str, old: str, new: str) -> str:
    import re
    return re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", new, text)


def _describe(term: Term) -> str:
    bits = [term.kind]
    if term.kind == "lathe":
        bits.append(f"r(z) = {term.profile}")
        bits.append(f"z in [{term.z_lo:g}, {term.z_hi:g}]")
        bits.append(f"cap {term.cap}")
        if term.clamp_radius and term.radius_cap > 0:
            bits.append(f"radius capped at {term.radius_cap:.2f} A by the box")
    elif term.kind == "sphere":
        where = ("group COM" if term.center_mode == "group"
                 else f"axis z = {term.center_z:g}")
        bits.append(f"R = {term.radius:g} A about {where}")
    elif term.kind == "slab":
        bits.append(f"z in [{term.z_lo:g}, {term.z_hi:g}]")
    else:
        bits.append("energy written by hand")
    if term.kind != "expression":
        bits.append(f"{term.mode}, k = {term.k:g}, "
                    f"exponent {term.exponent}")
    if term.is_off_axis:
        bits.append(f"origin offset axis {term.offset_axial:+.3f}, "
                    f"e1 {term.offset_e1:+.3f}, e2 {term.offset_e2:+.3f} A")
    if term.is_tilted:
        bits.append(f"tilted {term.tilt_deg:.3f} deg at azimuth "
                    f"{term.azimuth_deg:.3f} deg")
    if term.region.enabled:
        bits.append(f"active {term.region.z_lo:g} < z < {term.region.z_hi:g} A"
                    + (f", taper {term.region.taper:g} A"
                       if term.region.taper > 0 else ", hard gate"))
    if term.target_kind == "selection" and term.target_indices:
        bits.append(f"on {len(term.target_indices)} atom(s)")
    return "; ".join(bits)


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------
def validate_term(term: Term, spec, model, Issue) -> list:
    """Desmond-level and physics-level checks for one term."""
    out = []
    tag = term.label or term.var
    if not term.var.isidentifier():
        out.append(Issue("error", "term",
                         f"{tag}: `{term.var}` is not a valid variable name",
                         "Use letters, digits and underscores, starting with a "
                         "letter."))
    if term.k <= 0 and term.kind != "expression":
        out.append(Issue("error", "term", f"{tag}: force constant must be "
                                          "positive", ""))
    if term.exponent < 1 or abs(term.exponent - round(term.exponent)) > 1e-9:
        out.append(Issue("error", "term",
                         f"{tag}: the exponent must be a positive integer",
                         "Desmond's `^` operator raises to integer powers "
                         "only."))
    if term.kind in ("lathe", "slab") and term.z_hi <= term.z_lo:
        out.append(Issue("error", "term",
                         f"{tag}: z_hi must be greater than z_lo", ""))
    if term.kind == "sphere" and term.radius <= 0:
        out.append(Issue("error", "term", f"{tag}: the radius must be "
                                          "positive", ""))
    if term.target_kind == "selection":
        if not term.target_indices:
            out.append(Issue("error", "term", f"{tag}: the target group is "
                                              "empty", ""))
        elif model is not None:
            n = model.st.n_atoms
            bad = [i for i in term.target_indices if i < 1 or i > n]
            if bad:
                out.append(Issue("error", "term",
                                 f"{tag}: target atom {bad[0]} is outside "
                                 f"1..{n}", ""))
    if term.kind == "sphere" and term.center_mode == "group" and \
            not term.center_indices:
        out.append(Issue("error", "term", f"{tag}: the sphere centre group is "
                                          "empty", ""))
    if term.is_off_axis and not spec.has_frame_ref:
        out.append(Issue("error", "term",
                         f"{tag}: placed {term.offset_e1:+.3f}/"
                         f"{term.offset_e2:+.3f} A off the axis with no frame "
                         "reference",
                         "Pick a reference group so e1 and e2 exist, or set "
                         "the offsets to zero."))

    if (term.is_off_axis or term.is_tilted) and not spec.has_frame_ref:
        out.append(Issue("error", "term",
                         f"{tag}: placed off the funnel axis without a frame "
                         "reference group",
                         "e1 and e2 would be undefined in the potential. Set "
                         "a reference group in the Shapes tab, or zero the "
                         "offsets and the tilt."))
    if term.is_tilted:
        out.append(Issue("info", "term",
                         f"{tag}: axis tilted {term.tilt_deg:.2f}° towards "
                         f"{term.azimuth_deg:.1f}°",
                         "The direction is a fixed combination of the "
                         "body-fixed axis, e1 and e2, so it follows the "
                         "protein."))

    # -- region continuity
    if term.region.enabled:
        if term.region.z_hi <= term.region.z_lo:
            out.append(Issue("error", "term",
                             f"{tag}: the active region is empty", ""))
        if term.region.taper <= 0:
            out.append(Issue("warning", "term",
                             f"{tag}: the region uses a hard gate",
                             "The energy jumps at the region edge, which "
                             "kicks the integrator. Give the taper a width of "
                             "1-3 A to make the switch smooth."))
        elif term.region.taper < 0.5:
            out.append(Issue("warning", "term",
                             f"{tag}: taper of {term.region.taper:g} A is very "
                             "narrow", "The switching force will be large."))

    # -- expression checks
    known = {n for n, _ in expression_variables(spec)}
    if term.kind == "expression":
        from .mexpr import check_expression
        problems = check_expression(term.expression, known)
        for p in problems:
            out.append(Issue("error", "term", f"{tag}: {p}",
                             "The expression must be valid Desmond "
                             "M-expression using only its own functions."))
    if term.kind == "lathe":
        from .mexpr import check_expression
        for p in check_expression(term.profile, known):
            out.append(Issue("error", "term", f"{tag}: profile {p}", ""))

    if model is None:
        return out

    # -- numeric behaviour
    ctx = FieldCtx(model, points=model.frame.lig_com[None, :])
    try:
        v0 = float(np.asarray(term.energy(ctx)).reshape(-1)[0])
    except Exception as exc:                       # pragma: no cover
        out.append(Issue("error", "term", f"{tag}: could not be evaluated",
                         str(exc)))
        return out
    if not np.isfinite(v0):
        out.append(Issue("error", "term",
                         f"{tag}: evaluates to a non-finite value at the "
                         "starting geometry", "Check the expression."))
    elif v0 > 1e-6:
        out.append(Issue("warning", "term",
                         f"{tag}: costs {v0:.3f} kcal/mol at t = 0",
                         "The ligand starts inside this term's wall, so it "
                         "will be pushed the moment the run starts."))
    if term.kind == "lathe":
        zs = np.linspace(term.z_lo, term.z_hi, 120)
        r = ctx.profile_radius(term, z=zs)
        if not np.all(np.isfinite(r)):
            out.append(Issue("error", "term",
                             f"{tag}: the profile does not evaluate over "
                             f"z in [{term.z_lo:g}, {term.z_hi:g}]", ""))
        else:
            if np.any(r <= 0):
                out.append(Issue("warning", "term",
                                 f"{tag}: the profile reaches zero or below "
                                 "inside its own z range",
                                 "It is clamped to zero, which pins the "
                                 "target onto the axis there."))
            if float(np.nanmax(r)) > 200:
                out.append(Issue("warning", "term",
                                 f"{tag}: the profile grows past 200 Å", ""))
        if term.region.enabled and term.cap != "none":
            out.append(Issue("warning", "term",
                             f"{tag}: the axial cap is inside a switched "
                             "region",
                             "The region switch scales the whole term, so the "
                             "cap fades out exactly where it is supposed to "
                             "close the volume. Set the cap to none and close "
                             "the volume with the funnel walls or a slab "
                             "term."))
        if term.cap == "none" and term.mode == "confine" and \
                not term.region.enabled:
            out.append(Issue("info", "term",
                             f"{tag}: the volume is open at both ends",
                             "The target can leave along the axis unless "
                             "another term stops it."))
    return out


def next_var(existing: list[str]) -> str:
    k = 1
    used = set(existing)
    while f"w{k}" in used:
        k += 1
    return f"w{k}"


DEFAULT_COLORS = [
    (0.45, 0.85, 0.95), (0.98, 0.68, 0.35), (0.62, 0.90, 0.55),
    (0.90, 0.55, 0.85), (0.95, 0.90, 0.45), (0.60, 0.62, 0.98),
]


def make_term(kind: str, spec, model=None, existing: list[str] | None = None) -> Term:
    """A sensible new term of the requested kind for the current funnel.

    ``existing`` may be left out: the names already spoken for by the spec's
    other shapes and diagnostics are then used, so a new shape can never take
    a name that is already assigned in the file.
    """
    if existing is None:
        existing = [t.var for t in getattr(spec, "terms", [])]
        existing += [d.var for d in getattr(spec, "diagnostics", [])]
    t = Term(var=next_var(existing), kind=kind)
    t.color = DEFAULT_COLORS[len(existing) % len(DEFAULT_COLORS)]
    # a new shape has to start somewhere sensible even when there is no funnel
    z_lo = float(getattr(spec, "z_min", 0.0))
    z_hi = float(getattr(spec, "z_max", 0.0))
    if not (z_hi - z_lo > 1e-6):
        z0 = 0.0
        if model is not None:
            try:
                z0, _rho0 = model.cv_of_ligand()
            except Exception:
                z0 = 0.0
        z_lo, z_hi = z0 - 8.0, z0 + 12.0
        if model is not None:
            try:
                lo, hi = model.axis_limits(margin=0.5)
                if hi > lo:
                    z_lo = float(min(max(z_lo, lo), hi - 1.0))
                    z_hi = float(max(min(z_hi, hi), z_lo + 1.0))
            except Exception:
                pass
    t.z_lo, t.z_hi = z_lo, z_hi
    t.region.z_lo, t.region.z_hi = z_lo, z_hi
    if kind == "lathe":
        t.label = "Lathe wall"
        t.profile = fmt(max(2.0, float(getattr(spec, "r_cyl", 0.0)) or 5.0))
        t.k = getattr(spec, "k_rad", 25.0)
        t.k_cap = getattr(spec, "k_z", 50.0)
        t.cap = "solvent"
    elif kind == "sphere":
        t.label = "Spherical well"
        t.center_z = round(0.5 * (z_lo + z_hi), 3)
        t.radius = round(max(6.0, 0.4 * (z_hi - z_lo)), 3)
        if model is not None:
            try:
                t.radius = round(min(t.radius,
                                     max(1.0, model.term_max_radius(t, 0.5))),
                                 3)
            except Exception:
                pass
        t.k = 10.0
    elif kind == "slab":
        t.label = "Axial window"
        t.k = getattr(spec, "k_z", 50.0)
    else:
        t.label = "Custom expression"
        t.expression = "0.5*10.0*(rho-8.0)^2"
        t.k = 0.0
    return t
