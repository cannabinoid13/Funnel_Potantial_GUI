"""The funnel / well-tempered-metadynamics model.

``FunnelSpec`` holds every number that appears in the ``.pot`` file, and
``FunnelModel`` combines a spec with a ``Structure`` to give

* the protein-relative funnel frame (site COM, core COM, axis, origin),
* the collective variables ``z`` and ``rho`` for any point or set of points,
* the flat-bottom wall potential and the well-tempered hill height,
* analytic meshes for the funnel boundary and for iso-potential shells,
* a validation report against the Desmond conventions and the solvent box.

Everything is vectorised so the 3-D view can sample the potential on a grid.
"""

from __future__ import annotations

import math
import copy
from dataclasses import dataclass, field, asdict

import numpy as np

# Boltzmann constant in kcal/(mol K):  R = 8.314462618 J/(mol K), 1 cal = 4.184 J
KB_KCAL = 8.314462618 / 4184.0
EPS_RHO = 1.0e-12


# ---------------------------------------------------------------------------
# selections
# ---------------------------------------------------------------------------
@dataclass
class Selection:
    """A named ``atomsel("atom. ...")`` group, stored as 1-based indices."""

    var: str
    indices: list[int] = field(default_factory=list)
    expression: str = ""          # optional human selection expression

    def copy(self) -> "Selection":
        return Selection(self.var, list(self.indices), self.expression)

    @property
    def n(self) -> int:
        return len(self.indices)

    def idx0(self) -> np.ndarray:
        return np.asarray(self.indices, dtype=np.int64) - 1

    def asl(self) -> str:
        return "atom. " + ",".join(str(i) for i in self.indices)


@dataclass
class Diagnostic:
    """A reported (unbiased) distance from the ligand COM to a probe group."""

    var: str                     # e.g. "tyr114_oh"  -> tyr114_oh_sel, tyr114_oh
    label: str                   # e.g. "Tyr114_OH"  -> Tyr114_OH_to_ligCOM_A
    indices: list[int] = field(default_factory=list)
    dist_var: str = ""           # e.g. "d_tyr114"; defaults to "d_<var>"
    expression: str = ""

    def __post_init__(self):
        if not self.dist_var:
            self.dist_var = "d_" + self.var

    def copy(self) -> "Diagnostic":
        return Diagnostic(self.var, self.label, list(self.indices),
                          self.dist_var, self.expression)

    @property
    def print_label(self) -> str:
        return f"{self.label}_to_ligCOM_A"


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------
DEFAULT_PRINTS: list[tuple[str, str]] = [
    ("funnel_z_A", "z"),
    ("funnel_rho_A", "rho"),
    ("funnel_radius_A", "r_allowed"),
    ("funnel_radial_excess_A", "rad_excess"),
    ("funnel_Vrad_kcalmol", "v_rad"),
    ("funnel_Vzwall_kcalmol", "v_z"),
    ("WT_current_hill_kcalmol", "hill"),
    ("WT_bias_kcalmol", "v_meta"),
    ("funnel_axis_length_A", "axis_len"),
    ("total_bias_kcalmol", "v_total"),
]

#: Scalar variables a print() statement may reference.
BASE_PRINT_VARS = ["z", "rho", "r_allowed", "rad_excess", "v_rad",
                   "low_excess", "high_excess", "v_z", "axis_len",
                   "v_old", "hill", "v_meta", "ktemp", "h0", "sigma_z",
                   "v_total"]


#: Names the generated potential already uses, so a new group can never
#: shadow one of them (the M-expression language allows a single assignment).
RESERVED_NAMES = {
    "lig", "site", "core", "fref", "lig_com", "site_com", "core_com",
    "fref_com", "fref_v", "fref_par", "fref_perp", "axis_raw", "axis_len",
    "axis", "axis0", "p1", "p2", "e1", "e1r", "e2", "origin", "d", "z",
    "perp", "rho", "z_cc", "r_cyl", "cone_slope", "r_allowed", "rad_excess",
    "k_rad", "v_rad", "z_min", "z_max", "k_z", "low_excess", "high_excess",
    "v_z", "ktemp", "h0", "sigma_z", "sigma_rho", "v_old", "hill", "v_meta",
    "v_total", "time",
}


def make_diagnostic(spec, label: str, indices) -> Diagnostic:
    """A reported distance whose variable names cannot clash with anything.

    The imported file may already use a name that looks unrelated to its
    group (the reference potential calls the Tyr114-OH distance ``d_tyr114``),
    so both the group variable and the distance variable are checked against
    everything currently in the potential.
    """
    used = set(RESERVED_NAMES)
    for d in spec.diagnostics:
        used |= {d.var, f"{d.var}_sel", d.dist_var}
    for t in spec.terms:
        used |= {t.var, t.energy_var}
    labels = {d.label for d in spec.diagnostics}

    base = "".join(ch if (ch.isalnum() or ch == "_") else "_"
                   for ch in str(label)).strip("_").lower() or "probe"
    if base[0].isdigit():
        base = "g" + base
    var, k = base, 2
    while var in used or f"{var}_sel" in used or f"d_{var}" in used:
        var = f"{base}_{k}"
        k += 1
    nice = str(label) or var
    lab, k = nice, 2
    while lab in labels:
        lab = f"{nice}_{k}"
        k += 1
    return Diagnostic(var=var, label=lab, indices=sorted(int(i)
                                                         for i in indices),
                      dist_var=f"d_{var}")


@dataclass
class FunnelSpec:
    """Every parameter of the funnel + well-tempered metadynamics potential."""

    # --- metadynamics declaration
    dimension: int = 1
    kernel_cutoff: float = 9.0        # kernel truncation, in units of sigma
    meta_first: float = 100.0         # ps before the first hill
    meta_interval: float = 2.0        # ps between hills
    kerseq_name: str = "$JOBNAME.kerseq"
    initial_kernels: str = ""

    # --- CV output
    cvseq_name: str = "$JOBNAME.cvseq"
    cv_first: float = 0.0
    cv_interval: float = 1.0

    # --- groups
    lig: Selection = field(default_factory=lambda: Selection("lig"))
    site: Selection = field(default_factory=lambda: Selection("site"))
    core: Selection = field(default_factory=lambda: Selection("core"))
    #: Optional third group that fixes a perpendicular direction, so shapes
    #: can be placed off the axis and still rotate with the protein.
    frame_ref: Selection = field(default_factory=lambda: Selection("fref"))

    # --- funnel frame
    origin_mode: str = "fraction"      # 'fraction' of axis_raw, or 'absolute' A
    origin_frac: float = 0.5
    origin_offset: float = 0.0         # only used when origin_mode == 'absolute'
    #: Sideways placement of the funnel origin in the body-fixed frame.  e1
    #: and e2 are perpendicular to the axis, so these leave z untouched and
    #: slide the funnel across the axis.
    origin_lat1: float = 0.0
    origin_lat2: float = 0.0
    #: Orientation of the funnel's own axis.  A tilt rotates the biased
    #: coordinate away from the CORE->SITE vector, in the body-fixed frame,
    #: so it still follows the protein.
    axis_tilt_deg: float = 0.0
    axis_azimuth_deg: float = 0.0

    # --- funnel shape
    #
    # A brand-new spec carries **no** restraint: `funnel_enabled` is False and
    # the geometry is zero.  Importing a job without a .pot must not invent a
    # funnel, and must not look like it remembered the previous one, so these
    # defaults are deliberately empty rather than "typical" values.  Real
    # numbers arrive either from parsing a .pot or from
    # :meth:`initialise_funnel`.
    funnel_enabled: bool = False
    z_cc: float = 0.0
    r_cyl: float = 0.0
    cone_slope: float = 0.0
    k_rad: float = 25.0
    z_min: float = 0.0
    z_max: float = 0.0
    k_z: float = 50.0

    # --- well-tempered bias
    temperature: float = 310.0
    gamma: float = 15.0
    ktemp: float = 8.624466483
    h0: float = 0.1
    sigma_z: float = 0.5
    sigma_rho: float = 0.5             # only used when dimension == 2

    # --- extra potential terms (any shape, any region)
    terms: list = field(default_factory=list)

    # --- reporting
    diagnostics: list[Diagnostic] = field(default_factory=list)
    prints: list[tuple[str, str]] = field(
        default_factory=lambda: list(DEFAULT_PRINTS))

    # --- annotation / provenance
    ligand_label: str = "LIG"
    header_comment: list[str] = field(default_factory=list)
    notes: dict[str, list[str]] = field(default_factory=dict)
    auto_comments: bool = True

    # --- byte-exact round trip bookkeeping
    literals: dict[str, str] = field(default_factory=dict)
    literal_values: dict[str, float] = field(default_factory=dict)
    source_path: str = ""
    source_text: str = ""
    # Lossless binding of imported expressions to the existing designer.
    # This is session data, never a replacement for the original source.
    source_adapter: dict = field(default_factory=dict)

    # ---------------------------------------------------------------
    def copy(self) -> "FunnelSpec":
        s = FunnelSpec(**{k: v for k, v in self.__dict__.items()
                          if k not in ("lig", "site", "core", "frame_ref",
                                       "diagnostics", "prints",
                                       "header_comment", "notes", "literals",
                                       "literal_values", "terms", "source_adapter")})
        s.source_adapter = copy.deepcopy(self.source_adapter)
        s.terms = [t.copy() for t in self.terms]
        s.lig = self.lig.copy()
        s.site = self.site.copy()
        s.core = self.core.copy()
        s.frame_ref = self.frame_ref.copy()
        s.diagnostics = [d.copy() for d in self.diagnostics]
        s.prints = [tuple(p) for p in self.prints]
        s.header_comment = list(self.header_comment)
        s.notes = {k: list(v) for k, v in self.notes.items()}
        s.literals = dict(self.literals)
        s.literal_values = dict(self.literal_values)
        return s

    def to_dict(self) -> dict:
        d = asdict(self)
        d["prints"] = [list(p) for p in self.prints]
        d["terms"] = [t.to_dict() for t in self.terms]
        return d

    @property
    def active_terms(self) -> list:
        return [t for t in self.terms if t.enabled]

    def available_print_vars(self) -> list[str]:
        out = list(BASE_PRINT_VARS)
        if not self.funnel_enabled:
            out = [v for v in out if v not in
                   ("r_allowed", "rad_excess", "v_rad", "low_excess",
                    "high_excess", "v_z")]
        if self.dimension >= 2:
            out.insert(out.index("sigma_z") + 1, "sigma_rho")
        for t in self.terms:
            out.append(t.energy_var)
        for d in self.diagnostics:
            out.append(d.dist_var)
        return out

    # --- well tempered helpers
    def ktemp_from_gamma(self) -> float:
        return (self.gamma - 1.0) * KB_KCAL * self.temperature

    def gamma_from_ktemp(self) -> float:
        denom = KB_KCAL * self.temperature
        return 1.0 + self.ktemp / denom if denom > 0 else float("nan")

    def sync_ktemp(self) -> None:
        """Recompute ktemp from gamma/T (drops the imported literal)."""
        self.ktemp = self.ktemp_from_gamma()
        self.literals.pop("ktemp", None)

    def sync_gamma(self) -> None:
        self.gamma = self.gamma_from_ktemp()

    @property
    def has_frame_ref(self) -> bool:
        return bool(self.frame_ref.indices)

    @property
    def axis_is_tilted(self) -> bool:
        return abs(float(self.axis_tilt_deg)) > 1e-9

    def uses_perp_frame(self) -> bool:
        """True when e1/e2 have to be written into the potential."""
        if self.origin_lat1 or self.origin_lat2 or self.axis_is_tilted:
            return True
        return any(t.enabled and t.needs_perp_frame for t in self.terms)

    @property
    def cone_angle_deg(self) -> float:
        return math.degrees(math.atan(self.cone_slope))

    def set_cone_angle_deg(self, deg: float) -> None:
        self.cone_slope = math.tan(math.radians(deg))

    # --- geometry helpers that need no structure
    def r_allowed(self, z):
        if self.source_adapter:
            from .source_adapter import source_radius
            return source_radius(self, z)
        z = np.asarray(z, dtype=np.float64)
        return self.r_cyl + np.where(self.z_cc - z > 0.0,
                                     (self.z_cc - z) * self.cone_slope, 0.0)

    def r_base(self) -> float:
        """Cone radius at the lower axial wall."""
        return float(self.r_allowed(self.z_min))

    def wall_potential(self, z, rho):
        """V_rad + V_z of the built-in funnel, for CV values z and rho."""
        if self.source_adapter:
            from .source_adapter import source_wall
            return source_wall(self, z, rho)
        z = np.asarray(z, dtype=np.float64)
        rho = np.asarray(rho, dtype=np.float64)
        if not self.funnel_enabled:
            return np.zeros(np.broadcast(z, rho).shape)
        exc = rho - self.r_allowed(z)
        exc = np.where(exc > 0.0, exc, 0.0)
        v_rad = 0.5 * self.k_rad * exc ** 2
        lo = np.where(self.z_min - z > 0.0, self.z_min - z, 0.0)
        hi = np.where(z - self.z_max > 0.0, z - self.z_max, 0.0)
        v_z = 0.5 * self.k_z * (lo ** 2 + hi ** 2)
        return v_rad + v_z

    def v_rad(self, z, rho):
        if self.source_adapter:
            from .source_adapter import source_radial
            return source_radial(self, z, rho)
        exc = np.asarray(rho, dtype=np.float64) - self.r_allowed(z)
        exc = np.where(exc > 0.0, exc, 0.0)
        return 0.5 * self.k_rad * exc ** 2

    def v_zwall(self, z):
        if self.source_adapter:
            from .source_adapter import source_axial
            return source_axial(self, z)
        z = np.asarray(z, dtype=np.float64)
        lo = np.where(self.z_min - z > 0.0, self.z_min - z, 0.0)
        hi = np.where(z - self.z_max > 0.0, z - self.z_max, 0.0)
        return 0.5 * self.k_z * (lo ** 2 + hi ** 2)

    def radial_offset_for(self, v: float) -> float:
        """Radial excess that produces a wall energy of ``v`` kcal/mol."""
        if self.k_rad <= 0:
            return float("inf")
        return math.sqrt(max(0.0, 2.0 * v / self.k_rad))

    def axial_offset_for(self, v: float) -> float:
        if self.k_z <= 0:
            return float("inf")
        return math.sqrt(max(0.0, 2.0 * v / self.k_z))

    def funnel_volume(self) -> float:
        """Volume of the allowed region, A^3 (cone frustum + cylinder)."""
        if self.source_adapter:
            zs = np.linspace(self.z_min, self.z_max, 1001)
            return float(np.trapz(math.pi * self.r_allowed(zs) ** 2, zs))
        zc = min(max(self.z_cc, self.z_min), self.z_max)
        r0 = float(self.r_allowed(self.z_min))
        r1 = float(self.r_allowed(zc))
        h_cone = max(0.0, zc - self.z_min)
        v_cone = math.pi * h_cone * (r0 * r0 + r0 * r1 + r1 * r1) / 3.0
        v_cyl = math.pi * self.r_cyl ** 2 * max(0.0, self.z_max - zc)
        return v_cone + v_cyl

    # --- deposition bookkeeping
    def hills_for_time(self, total_ps: float) -> int:
        if self.meta_interval <= 0 or total_ps <= self.meta_first:
            return 0
        return int(math.floor((total_ps - self.meta_first) / self.meta_interval)) + 1

    def max_bias_estimate(self, total_ps: float) -> float:
        """Rough saturation of a WT bias: gamma-scaled well depth guess."""
        n = self.hills_for_time(total_ps)
        return self.h0 * n            # unbiased upper bound (before tempering)


# ---------------------------------------------------------------------------
# frame + model
# ---------------------------------------------------------------------------
@dataclass
class Frame:
    site_com: np.ndarray
    core_com: np.ndarray
    lig_com: np.ndarray
    axis_raw: np.ndarray
    axis_len: float
    axis: np.ndarray
    origin: np.ndarray
    e1: np.ndarray
    e2: np.ndarray

    def cyl_to_world(self, z, rho, phi):
        z = np.asarray(z, dtype=np.float64)
        rho = np.asarray(rho, dtype=np.float64)
        phi = np.asarray(phi, dtype=np.float64)
        return (self.origin
                + z[..., None] * self.axis
                + (rho * np.cos(phi))[..., None] * self.e1
                + (rho * np.sin(phi))[..., None] * self.e2)


@dataclass
class Issue:
    level: str          # 'error' | 'warning' | 'info' | 'ok'
    category: str
    message: str
    detail: str = ""


class FunnelModel:
    """Spec + structure = the concrete funnel restraint for this system."""

    def __init__(self, structure, spec: FunnelSpec):
        self.st = structure
        self.spec = spec
        self._frame: Frame | None = None

    def invalidate(self) -> None:
        self._frame = None

    # -- frame -------------------------------------------------------
    @property
    def frame(self) -> Frame:
        if self._frame is None:
            self._frame = self._build_frame()
        return self._frame

    def _build_frame(self) -> Frame:
        st, sp = self.st, self.spec
        if sp.source_adapter:
            from .source_adapter import source_frame
            return source_frame(sp, st)
        site_com = st.center_of_mass(sp.site.idx0())
        core_com = st.center_of_mass(sp.core.idx0())
        lig_com = st.center_of_mass(sp.lig.idx0())
        axis_raw = st.min_image(site_com - core_com)
        axis_len = float(np.linalg.norm(axis_raw))
        if axis_len <= 1e-9:
            axis = np.array([0.0, 0.0, 1.0])
            axis_len = 1.0
            axis_raw = axis.copy()
        else:
            axis = axis_raw / axis_len
        if sp.origin_mode == "absolute":
            origin = core_com + sp.origin_offset * axis
        else:
            origin = core_com + sp.origin_frac * axis_raw
        # Perpendicular basis.  With a reference group it is body-fixed, so
        # it is the same basis the exported potential uses and an off-axis or
        # tilted placement means the same thing on screen and in the run.
        p1 = None
        if sp.frame_ref.indices:
            try:
                ref_com = st.center_of_mass(sp.frame_ref.idx0())
                v = st.min_image(ref_com - core_com)
                perp = v - float(v @ axis) * axis
                n = float(np.linalg.norm(perp))
                if n > 1e-6:
                    p1 = perp / n
            except Exception:
                p1 = None
        if p1 is None:
            tmp = np.zeros(3)
            tmp[int(np.argmin(np.abs(axis)))] = 1.0
            p1 = np.cross(axis, tmp)
            p1 /= np.linalg.norm(p1)
        p2 = np.cross(axis, p1)
        if sp.axis_is_tilted:
            from .terms import unit_from_tilt
            axis = unit_from_tilt(axis, p1, p2, sp.axis_tilt_deg,
                                  sp.axis_azimuth_deg)
            e1 = p1 - float(p1 @ axis) * axis
            n1 = float(np.linalg.norm(e1))
            e1 = e1 / n1 if n1 > 1e-9 else p2
        else:
            e1 = p1
        e2 = np.cross(axis, e1)
        origin = origin + sp.origin_lat1 * e1 + sp.origin_lat2 * e2
        return Frame(site_com, core_com, lig_com, axis_raw, axis_len, axis,
                     origin, e1, e2)

    @property
    def origin_offset_A(self) -> float:
        sp = self.spec
        if sp.origin_mode == "absolute":
            return sp.origin_offset
        return sp.origin_frac * self.frame.axis_len

    def set_origin_offset_A(self, value: float) -> None:
        sp = self.spec
        if sp.origin_mode == "absolute":
            sp.origin_offset = value
        else:
            L = self.frame.axis_len
            sp.origin_frac = value / L if L > 0 else 0.0
            sp.literals.pop("origin_frac", None)
        self.invalidate()

    # -- creating a funnel where there was none -----------------------
    # -- staying inside the supplied solvent box -----------------------
    def box_planes(self):
        """(centre, unit normals, half-thicknesses) of the periodic cell."""
        st = self.st
        centre = st.box_center
        H = st.box
        normals, halves = [], []
        for i in range(3):
            n = np.cross(H[(i + 1) % 3], H[(i + 2) % 3])
            ln = float(np.linalg.norm(n))
            if ln < 1e-12:
                continue
            n = n / ln
            normals.append(n)
            halves.append(abs(float(H[i] @ n)) / 2.0)
        return np.asarray(centre), np.asarray(normals), np.asarray(halves)

    def box_limits_along(self, base, direction, radius: float = 0.0,
                         margin: float = 0.0):
        """How far ``base + t*direction`` may run and stay inside the box.

        ``radius`` is the size of the shape carried along that line, so the
        whole shape - not just its centre - stays inside.
        """
        centre, normals, halves = self.box_planes()
        base = np.asarray(base, dtype=float)
        d = np.asarray(direction, dtype=float)
        nd = float(np.linalg.norm(d))
        if nd < 1e-12:
            return (0.0, 0.0)
        d = d / nd
        lo, hi = -1e9, 1e9
        for n, h in zip(normals, halves):
            H = h - radius - margin
            if H <= 0.0:
                # a shape that big does not fit in the cell at all
                return (0.0, 0.0)
            A = float((base - centre) @ n)
            B = float(d @ n)
            if abs(B) < 1e-9:
                if abs(A) > H:
                    return (0.0, 0.0)
                continue
            t1 = (-H - A) / B
            t2 = (H - A) / B
            lo = max(lo, min(t1, t2))
            hi = min(hi, max(t1, t2))
        if hi < lo:
            return (0.0, 0.0)
        return (float(lo), float(hi))

    def room_at(self, z, margin: float = 0.0) -> np.ndarray:
        """Radius that fits inside the box at each axial position."""
        centre, normals, halves = self.box_planes()
        fr = self.frame
        zs = np.atleast_1d(np.asarray(z, dtype=float))
        pts = fr.origin + zs[:, None] * fr.axis
        best = np.full(zs.shape, np.inf)
        for n, h in zip(normals, halves):
            best = np.minimum(best, h - np.abs((pts - centre) @ n) - margin)
        return np.maximum(best, 0.0)

    def max_radius_at(self, z, margin: float = 0.0,
                      reduce: str = "min") -> float:
        """Radius that fits over a stretch of the axis ('min') or at its
        most generous point ('max')."""
        room = self.room_at(z, margin)
        return float(room.max() if reduce == "max" else room.min())

    def best_room(self, margin: float = 0.0) -> float:
        """The largest radius the cell can hold anywhere along the axis."""
        lo, hi = self.axis_limits(margin=margin)
        if hi <= lo:
            return 0.0
        return self.max_radius_at(np.linspace(lo, hi, 64), margin,
                                  reduce="max")

    def term_profile_max(self, term, margin: float = 0.0) -> float:
        """Widest radius a lathe term's profile reaches over its own range."""
        if term.kind == "sphere":
            return float(term.radius)
        if term.kind != "lathe":
            return 0.0
        zs = np.linspace(term.z_lo, term.z_hi, 32)
        r = self.term_profile(term, zs)
        if r is None or not np.any(np.isfinite(r)):
            return 0.0
        return float(np.nanmax(np.where(np.isfinite(r), r, 0.0)))

    def axis_limits(self, radius: float = 0.0, margin: float = 0.0):
        return self.box_limits_along(self.frame.origin, self.frame.axis,
                                     radius, margin)

    def lateral_limits(self, which: int = 1, radius: float = 0.0,
                       margin: float = 0.0):
        fr = self.frame
        d = fr.e1 if which == 1 else fr.e2
        return self.box_limits_along(fr.origin, d, radius, margin)

    def term_max_radius(self, term, margin: float = 0.0) -> float:
        """Largest radius a shape may have where it currently sits."""
        fr = self.frame
        if term.kind == "sphere":
            zs = np.array([term.center_z])
        else:
            zs = np.linspace(term.z_lo, term.z_hi, 24)
        base = (fr.origin + term.offset_e1 * fr.e1 + term.offset_e2 * fr.e2)
        centre, normals, halves = self.box_planes()
        pts = base + zs[:, None] * fr.axis
        best = np.inf
        for n, h in zip(normals, halves):
            best = min(best, float(np.min(h - np.abs((pts - centre) @ n))))
        return max(0.0, best - margin)

    def term_overshoot(self, term, margin: float = 0.0) -> float:
        """How far this one shape reaches outside the cell, in A."""
        centre, normals, halves = self.box_planes()
        if term.kind == "slab":
            o, u, _v1, _v2 = self.term_frame(term)
            pts = np.array([o + term.z_lo * u, o + term.z_hi * u])
        else:
            pts = self.term_surface_points(term, n_z=16, n_theta=12)
        if not len(pts):
            return 0.0
        worst = 0.0
        for n, h in zip(normals, halves):
            d = np.abs((pts - centre) @ n) - (h - margin)
            worst = max(worst, float(np.max(d)))
        return max(0.0, worst)

    def funnel_overshoot(self, margin: float = 0.0) -> float:
        sp = self.spec
        if not (sp.funnel_enabled and not self.funnel_is_degenerate()):
            return 0.0
        centre, normals, halves = self.box_planes()
        fr = self.frame
        zs = np.linspace(sp.z_min, sp.z_max, 32)
        theta = np.linspace(0, 2 * np.pi, 16, endpoint=False)
        ZZ, TT = np.meshgrid(zs, theta, indexing="ij")
        RR = np.repeat(sp.r_allowed(zs)[:, None], len(theta), axis=1)
        pts = fr.cyl_to_world(ZZ.ravel(), RR.ravel(), TT.ravel())
        worst = 0.0
        for n, h in zip(normals, halves):
            d = np.abs((pts - centre) @ n) - (h - margin)
            worst = max(worst, float(np.max(d)))
        return max(0.0, worst)

    def clamp_tilts(self, margin: float = 0.25) -> list:
        """Reduce any tilt that swings a shape out of the cell."""
        sp = self.spec
        changed = []
        if sp.axis_is_tilted and self.funnel_overshoot(margin) > 1e-6:
            lo, hi = 0.0, float(sp.axis_tilt_deg)
            for _ in range(12):
                mid = 0.5 * (lo + hi)
                sp.axis_tilt_deg = mid
                self.invalidate()
                if self.funnel_overshoot(margin) > 1e-6:
                    hi = mid
                else:
                    lo = mid
            if abs(lo - hi) < 1e9:
                changed.append(f"funnel axis tilt {hi:.3f} → {lo:.3f} deg "
                               "(the box)")
            sp.axis_tilt_deg = lo
            self.invalidate()
        for t in sp.terms:
            if not (t.enabled and t.is_tilted):
                continue
            if self.term_overshoot(t, margin) <= 1e-6:
                continue
            lo, hi = 0.0, float(t.tilt_deg)
            for _ in range(12):
                mid = 0.5 * (lo + hi)
                t.tilt_deg = mid
                if self.term_overshoot(t, margin) > 1e-6:
                    hi = mid
                else:
                    lo = mid
            changed.append(f"{t.label} tilt {hi:.3f} → {lo:.3f} deg (the box)")
            t.tilt_deg = lo
        return changed

    def pull_inside(self, term, margin: float = 0.25,
                    iterations: int = 16) -> bool:
        """Push one shape back until its real surface is inside the cell.

        This is the backstop behind the per-parameter limits: it measures the
        body that is actually drawn - tilted, offset, capped, whatever - and
        moves it along the offending face normal, shrinking it only if moving
        is not enough.  It always terminates.
        """
        centre, normals, halves = self.box_planes()
        fr = self.frame
        moved = False
        for k in range(iterations):
            if term.kind == "slab":
                o, u, _v1, _v2 = self.term_frame(term)
                pts = np.array([o + term.z_lo * u, o + term.z_hi * u])
            else:
                pts = self.term_surface_points(term, n_z=16, n_theta=12)
            if not len(pts):
                return moved
            worst, wn, wsign = 0.0, None, 1.0
            for n, h in zip(normals, halves):
                proj = (pts - centre) @ n
                for sign in (1.0, -1.0):
                    d = float(np.max(sign * proj)) - (h - margin)
                    if d > worst:
                        worst, wn, wsign = d, n, sign
            if worst <= 1e-6 or wn is None:
                return moved
            shift = -wsign * worst * 1.02 * wn
            term.offset_axial += float(shift @ fr.axis)
            term.offset_e1 += float(shift @ fr.e1)
            term.offset_e2 += float(shift @ fr.e2)
            moved = True
            if k >= 8:
                # moving is not winning: make the body smaller as well
                if term.kind == "sphere":
                    term.radius = max(0.1, term.radius * 0.85)
                else:
                    mid = 0.5 * (term.z_lo + term.z_hi)
                    half = 0.85 * 0.5 * (term.z_hi - term.z_lo)
                    term.z_lo, term.z_hi = mid - half, mid + half
                    if term.kind == "lathe":
                        room = self.term_max_radius(term, margin)
                        if self.term_profile_max(term) > room:
                            term.clamp_radius = True
                            term.radius_cap = round(max(0.1, room), 3)
        return moved

    def clamp_to_box(self, margin: float = 0.25, passes: int = 4) -> list:
        """Pull the funnel and every shape back inside the solvent box.

        Radii are reduced first, then the axial extent is limited using the
        shape's own radius, so the whole body stays inside rather than just
        its centre line.  A shape with no room left is moved towards the
        middle of the cell instead of being shrunk to nothing.  This runs
        after a user edit, never on import: an imported potential is reported
        as it is and flagged by the validator rather than silently rewritten.
        """
        sp = self.spec
        changed: list[str] = []
        room_any = self.best_room(margin)
        MIN_SPAN = 0.5   # A; the shortest window a shape may be squeezed to

        def note(what, old, new, unit=" A"):
            if abs(float(old) - float(new)) > 1e-9:
                changed.append(f"{what} {float(old):.3f} → {float(new):.3f}"
                               f"{unit}")

        def clamp_axis(value, radius):
            lo, hi = self.axis_limits(radius=radius, margin=margin)
            if hi <= lo:
                lo, hi = self.axis_limits(margin=margin)
                return 0.5 * (lo + hi)
            return float(np.clip(value, lo, hi))

        for _ in range(max(1, passes)):
            if sp.has_frame_ref:
                for which, attr in ((1, "origin_lat1"), (2, "origin_lat2")):
                    lo, hi = self.lateral_limits(which, margin=margin)
                    v = float(getattr(sp, attr))
                    nv = float(np.clip(v, lo, hi)) if hi > lo else 0.0
                    note(f"funnel {attr}", v, nv)
                    setattr(sp, attr, nv)
                self.invalidate()
            elif sp.origin_lat1 or sp.origin_lat2:
                sp.origin_lat1 = sp.origin_lat2 = 0.0

            if sp.funnel_enabled and not self.funnel_is_degenerate():
                # 1. radii the cell can actually hold
                new_r = float(min(sp.r_cyl, room_any))
                note("r_cyl", sp.r_cyl, new_r)
                sp.r_cyl = max(0.05, new_r)
                # 2. the two ends, each carrying its own radius
                z_max = clamp_axis(sp.z_max, sp.r_cyl)
                note("z_max", sp.z_max, z_max)
                sp.z_max = z_max
                z_min = clamp_axis(sp.z_min, min(sp.r_base(), room_any))
                if z_min > sp.z_max - MIN_SPAN:
                    z_min = clamp_axis(sp.z_max - 1.0, sp.r_cyl)
                if z_min > sp.z_max - MIN_SPAN:
                    z_min = sp.z_max - MIN_SPAN
                note("z_min", sp.z_min, z_min)
                sp.z_min = z_min
                sp.z_cc = float(np.clip(sp.z_cc, sp.z_min, sp.z_max))
                self.invalidate()
                # 3. the cone may not lean out of the box
                base_room = self.max_radius_at(np.array([sp.z_min]), margin)
                span = max(sp.z_cc - sp.z_min, 1e-6)
                slope_max = max(0.0, (base_room - sp.r_cyl) / span)
                if sp.cone_slope > slope_max:
                    changed.append(f"cone slope {sp.cone_slope:.4f} → "
                                   f"{slope_max:.4f}")
                    sp.cone_slope = slope_max
                self.invalidate()

            for t in sp.terms:
                if not sp.has_frame_ref:
                    if t.offset_e1 or t.offset_e2:
                        t.offset_e1 = t.offset_e2 = 0.0
                        changed.append(f"{t.label} sideways offsets cleared "
                                       "(no frame reference)")
                else:
                    r_body = (t.radius if t.kind == "sphere"
                              else (0.0 if t.kind == "slab"
                                    else self.term_profile_max(t, margin)))
                    r_body = min(r_body, room_any)
                    for which, attr in ((1, "offset_e1"), (2, "offset_e2")):
                        lo, hi = self.lateral_limits(which, radius=r_body,
                                                     margin=margin)
                        if hi <= lo:
                            lo, hi = self.lateral_limits(which, margin=margin)
                        v = float(getattr(t, attr))
                        nv = float(np.clip(v, lo, hi)) if hi > lo else 0.0
                        if abs(v - nv) > 1e-9:
                            changed.append(f"{t.label} {attr} {v:.3f} → "
                                           f"{nv:.3f} A")
                        setattr(t, attr, nv)
                if t.kind == "sphere":
                    if t.radius > room_any:
                        note(f"{t.label} radius", t.radius, room_any)
                        t.radius = max(0.05, float(room_any))
                    o, u, _v1, _v2 = self.term_frame(t)
                    lo, hi = self.box_limits_along(o, u, radius=t.radius,
                                                   margin=margin)
                    cz = (float(np.clip(t.center_z, lo, hi)) if hi > lo
                          else 0.5 * sum(self.box_limits_along(o, u,
                                                               margin=margin)))
                    note(f"{t.label} centre", t.center_z, cz)
                    t.center_z = cz
                    room = self.term_max_radius(t, margin)
                    if t.radius > room:
                        note(f"{t.label} radius", t.radius, room)
                        t.radius = max(0.05, float(room))
                else:
                    r_env = min(self.term_profile_max(t, margin), room_any)
                    o, u, _v1, _v2 = self.term_frame(t)
                    lo, hi = self.box_limits_along(o, u, radius=r_env,
                                                   margin=margin)
                    if hi <= lo:
                        lo, hi = self.box_limits_along(o, u, margin=margin)
                    z_lo = float(np.clip(t.z_lo, lo, hi))
                    z_hi = float(np.clip(t.z_hi, lo, hi))
                    # never let the window collapse or turn inside out: a
                    # shape squeezed against a box face keeps a minimum
                    # length and slides, rather than ending up with
                    # z_hi <= z_lo, which is not a shape at all
                    if z_hi - z_lo < MIN_SPAN:
                        if hi - lo >= MIN_SPAN:
                            z_lo = min(z_lo, hi - MIN_SPAN)
                            z_hi = z_lo + MIN_SPAN
                        else:
                            mid = 0.5 * (lo + hi)
                            z_lo = mid - 0.5 * MIN_SPAN
                            z_hi = mid + 0.5 * MIN_SPAN
                    if abs(z_lo - t.z_lo) > 1e-9 or abs(z_hi - t.z_hi) > 1e-9:
                        changed.append(
                            f"{t.label} z {t.z_lo:.3f}…{t.z_hi:.3f} → "
                            f"{z_lo:.3f}…{z_hi:.3f} A")
                    t.z_lo, t.z_hi = z_lo, z_hi
                    # a profile can still be too wide for the cell; the wall
                    # is then capped where it is written, not just here
                    if t.kind == "lathe":
                        room = self.term_max_radius(t, margin)
                        if self.term_profile_max(t) > room + 1e-6:
                            if not t.clamp_radius:
                                t.clamp_radius = True
                                changed.append(
                                    f"{t.label}: profile is wider than the "
                                    "box, so its radius is now capped at "
                                    f"{room:.2f} A in the potential")
                            t.radius_cap = round(float(room), 3)
        changed += self.clamp_tilts(margin)
        for t in sp.terms:
            if not t.enabled:
                continue
            if self.term_overshoot(t, margin) > 1e-6:
                if self.pull_inside(t, margin):
                    changed.append(f"{t.label} moved back inside the box")
        seen, out = set(), []
        for c in changed:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def outside_box(self, margin: float = 0.0) -> list:
        """What currently sticks out of the solvent box, if anything."""
        sp = self.spec
        out: list[str] = []
        centre, normals, halves = self.box_planes()

        def check(label, pts):
            worst = 0.0
            for n, h in zip(normals, halves):
                d = np.abs((pts - centre) @ n) - (h - margin)
                worst = max(worst, float(np.max(d)))
            if worst > 1e-6:
                out.append(f"{label} sticks out by {worst:.2f} A")

        if sp.funnel_enabled and not self.funnel_is_degenerate():
            fr = self.frame
            zs = np.linspace(sp.z_min, sp.z_max, 48)
            theta = np.linspace(0, 2 * np.pi, 24, endpoint=False)
            ZZ, TT = np.meshgrid(zs, theta, indexing="ij")
            RR = np.repeat(sp.r_allowed(zs)[:, None], len(theta), axis=1)
            check("the funnel envelope",
                  fr.cyl_to_world(ZZ.ravel(), RR.ravel(), TT.ravel()))
        for t in sp.active_terms:
            if t.kind == "slab":
                # two planes perpendicular to the axis: they have no radial
                # extent to stick out, only their positions matter
                o, u, _v1, _v2 = self.term_frame(t)
                check(f"'{t.label}'", np.array([o + t.z_lo * u,
                                                o + t.z_hi * u]))
                continue
            pts = self.term_surface_points(t)
            if len(pts):
                check(f"'{t.label}'", pts)
        return out

    def free_radius_profile(self, z_from: float, z_to: float,
                            step: float = 0.25):
        """(z, r_free): how far the axis is from the nearest solute atom.

        This is the solvent-accessible radius around the axis, and it is what
        the automatic funnel placement is fitted to: the cone should stay
        inside it so the wall never cuts through the protein, and the bulk
        cylinder should start where it stops being blocked.
        """
        st = self.st
        fr = self.frame
        zs = np.arange(z_from, z_to, step)
        if zs.size == 0:
            return zs, zs
        pts = fr.origin + zs[:, None] * fr.axis
        heavy = np.where(st.mask("heavy") & ~st.mask("water"))[0]
        if heavy.size == 0:
            return zs, np.full_like(zs, np.inf)
        try:
            from scipy.spatial import cKDTree
            d, _ = cKDTree(st.xyz[heavy]).query(pts, k=1)
        except ImportError:
            d = np.array([np.linalg.norm(st.xyz[heavy] - p, axis=1).min()
                          for p in pts])
        return zs, np.asarray(d, dtype=np.float64)

    def initialise_funnel(self, cutoff: float = 9.0, r_cyl: float = 4.0,
                          clear_margin: float = 1.5,
                          bulk_run: float = 10.0,
                          pocket_pad: float = 3.0,
                          bulk_pad: float = 8.0,
                          cone_deg: float = 30.0) -> dict:
        """Give the built-in funnel a geometry fitted to this pocket.

        Nothing is invented until this is called - a job imported without a
        .pot has no funnel at all.  The fit is:

        * ``z_min``  = ``pocket_pad`` below the ligand, rounded down;
        * ``z_cc``   = the first z from which the axis stays clear of solute by
          more than ``r_cyl + clear_margin`` for ``bulk_run`` A, i.e. where the
          channel really opens into bulk;
        * ``cone``   = 30 degrees, the usual funnel-metadynamics choice,
          widened if the ligand would not otherwise start free.  It is *not*
          fitted to the solvent-accessible radius: a funnel cone legitimately
          encloses protein atoms, since the wall only ever acts on the ligand
          and the ligand cannot occupy those positions anyway;
        * ``z_max``  = ``bulk_pad`` past ``z_cc``, pulled back until the whole
          envelope clears every box face by the non-bonded cutoff.
        """
        sp = self.spec
        z0, rho0 = self.cv_of_ligand()
        zs, r_free = self.free_radius_profile(z0 - pocket_pad - 2.0,
                                             z0 + 100.0)
        need = r_cyl + clear_margin
        win = max(1, int(round(bulk_run / 0.25)))
        z_cc = None
        start = int(np.searchsorted(zs, z0))
        for k in range(start, max(start + 1, len(zs) - win)):
            if np.all(r_free[k:k + win] > need):
                z_cc = float(zs[k])
                break
        opened = z_cc is not None
        if z_cc is None:
            z_cc = float(zs[int(np.argmax(r_free))]) if zs.size else z0 + 20.0
        z_min = float(np.floor(z0 - pocket_pad))
        span = max(z_cc - z_min, 1e-6)

        slope_lo = math.tan(math.radians(15.0))
        slope_hi = math.tan(math.radians(60.0))
        slope = math.tan(math.radians(cone_deg))
        # widen only if the ligand would not start free, with room to move
        need_slope = (rho0 + 3.0 - r_cyl) / max(z_cc - z0, 1e-6)
        if need_slope > slope:
            slope = need_slope
        slope = float(np.clip(slope, slope_lo, slope_hi * 1.5))

        sp.funnel_enabled = True
        sp.r_cyl = r_cyl
        sp.z_min = z_min
        sp.z_cc = z_cc
        sp.cone_slope = slope
        if sp.k_rad <= 0:
            sp.k_rad = 25.0
        if sp.k_z <= 0:
            sp.k_z = 50.0
        z_max = z_cc + bulk_pad
        pulled = 0.0
        for _ in range(200):
            sp.z_max = z_max
            self.invalidate()
            clear, _p = self.funnel_clearance(include_terms=False)
            if not np.isfinite(clear) or clear >= cutoff or \
                    z_max <= z_cc + 1.0:
                break
            z_max -= 0.5
            pulled += 0.5
        for key in ("z_min", "z_max", "z_cc", "cone_slope", "r_cyl"):
            sp.literals.pop(key, None)
        self.invalidate()
        clear, _p = self.funnel_clearance(include_terms=False)
        z_now, rho_now = self.cv_of_ligand()
        return {"z0": z_now, "rho0": rho_now, "z_min": sp.z_min,
                "z_cc": sp.z_cc, "z_max": sp.z_max,
                "cone_deg": sp.cone_angle_deg, "r_base": sp.r_base(),
                "r_cyl": sp.r_cyl, "clearance": clear, "pulled_back": pulled,
                "channel_opened": opened,
                "free_at_zcc": float(np.interp(sp.z_cc, zs, r_free))
                if zs.size else float("nan"),
                "atoms_in_funnel": int(len(self.atoms_in_funnel(
                    self.st.mask("heavy") & ~self.st.mask("water")))),
                "start_energy": float(self.total_at_ligand())}

    def funnel_is_degenerate(self) -> bool:
        sp = self.spec
        return (sp.z_max - sp.z_min) <= 1e-9 or sp.r_cyl <= 0.0

    # -- collective variables ----------------------------------------
    def cv(self, points=None):
        """Return (z, rho) for world-space ``points`` (default: ligand COM)."""
        if self.spec.source_adapter:
            from .source_adapter import source_cvs
            single = points is None or np.asarray(points).ndim == 1
            z, rho = source_cvs(self.spec, self.st,
                                None if points is None else np.atleast_2d(points))
            if single:
                return float(np.asarray(z).reshape(-1)[0]), float(np.asarray(rho).reshape(-1)[0])
            return np.asarray(z), np.asarray(rho)
        fr = self.frame
        if points is None:
            points = fr.lig_com[None, :]
            single = True
        else:
            points = np.asarray(points, dtype=np.float64)
            single = points.ndim == 1
            if single:
                points = points[None, :]
        d = self.st.min_image(points - fr.origin)
        z = d @ fr.axis
        perp = d - z[:, None] * fr.axis
        rho = np.sqrt(np.einsum("ij,ij->i", perp, perp) + EPS_RHO)
        if single:
            return float(z[0]), float(rho[0])
        return z, rho

    def cv_of_ligand(self) -> tuple[float, float]:
        return self.cv(None)

    def wall_at_points(self, points) -> np.ndarray:
        z, rho = self.cv(points)
        return self.spec.wall_potential(z, rho)

    # -- the whole restraint: built-in funnel plus every extra term
    def field_ctx(self, points=None, z=None, rho=None):
        from .terms import FieldCtx
        return FieldCtx(self, points=points, z=z, rho=rho)

    def term_field(self, points=None, z=None, rho=None, ctx=None):
        """Sum of the extra terms over a sample of positions."""
        terms = self.spec.active_terms
        if not terms:
            base = ctx.z if ctx is not None else (
                np.atleast_1d(np.asarray(z, dtype=np.float64))
                if z is not None else None)
            if base is None:
                ctx = self.field_ctx(points=points)
                base = ctx.z
            return np.zeros_like(base)
        ctx = ctx or self.field_ctx(points=points, z=z, rho=rho)
        total = np.zeros_like(ctx.z)
        for t in terms:
            v = np.asarray(t.energy(ctx), dtype=np.float64)
            total = total + np.where(np.isfinite(v), v, 0.0)
        return total

    def total_field(self, points=None, z=None, rho=None):
        """Funnel walls plus all extra terms (no metadynamics bias)."""
        if self.spec.source_adapter:
            from .source_adapter import source_field
            if points is None:
                if z is None or rho is None:
                    points = self.frame.lig_com[None, :]
                else:
                    zz, rr = np.broadcast_arrays(z, rho)
                    points = self.frame.cyl_to_world(zz.ravel(), rr.ravel(), 0.0)
            return source_field(self.spec, self.st, np.atleast_2d(points))
        ctx = self.field_ctx(points=points, z=z, rho=rho)
        v = np.asarray(self.spec.wall_potential(ctx.z, ctx.rho),
                       dtype=np.float64)
        return v + self.term_field(ctx=ctx)

    def total_at_ligand(self) -> float:
        z0, rho0 = self.cv_of_ligand()
        return float(np.asarray(self.total_field(
            points=self.frame.lig_com[None, :])).reshape(-1)[0])

    def term_energies_at_ligand(self) -> list[tuple[str, float]]:
        ctx = self.field_ctx(points=self.frame.lig_com[None, :])
        out = []
        for t in self.spec.terms:
            try:
                v = float(np.asarray(t.energy(ctx)).reshape(-1)[0])
            except Exception:
                v = float("nan")
            out.append((t.label or t.var, v))
        return out

    # -- meshes ------------------------------------------------------
    def revolve_mesh(self, zline, rline, n_theta: int = 96):
        """Turn a profile r(z) on a lathe about the funnel axis."""
        zline = np.asarray(zline, dtype=np.float64)
        rline = np.asarray(rline, dtype=np.float64)
        rline = np.where(np.isfinite(rline), rline, 0.0)
        rline = np.where(rline > 0.0, rline, 0.0)
        theta = np.linspace(0.0, 2.0 * np.pi, n_theta, endpoint=False)
        ZZ, TT = np.meshgrid(zline, theta, indexing="ij")
        RR = np.repeat(rline[:, None], n_theta, axis=1)
        pts = self.frame.cyl_to_world(ZZ.ravel(), RR.ravel(), TT.ravel())
        nz = len(zline)
        faces = []
        for i in range(nz - 1):
            for j in range(n_theta):
                j2 = (j + 1) % n_theta
                a = i * n_theta + j
                b = i * n_theta + j2
                c = (i + 1) * n_theta + j2
                d = (i + 1) * n_theta + j
                faces.append((a, b, c))
                faces.append((a, c, d))
        return pts, np.asarray(faces, dtype=np.int64)

    def disc_mesh(self, z: float, radius: float, n_theta: int = 96):
        """A flat cap perpendicular to the axis."""
        theta = np.linspace(0.0, 2.0 * np.pi, n_theta, endpoint=False)
        rim = self.frame.cyl_to_world(np.full(n_theta, z),
                                      np.full(n_theta, max(radius, 1e-6)),
                                      theta)
        centre = self.frame.cyl_to_world(np.array([z]), np.array([0.0]),
                                         np.array([0.0]))
        pts = np.vstack([centre, rim])
        faces = [(0, 1 + j, 1 + (j + 1) % n_theta) for j in range(n_theta)]
        return pts, np.asarray(faces, dtype=np.int64)

    def term_frame(self, term):
        """(origin, axis, perp1, perp2) of a shape's own coordinate system."""
        from .terms import unit_from_tilt
        fr = self.frame
        origin = (fr.origin + term.offset_axial * fr.axis
                  + term.offset_e1 * fr.e1 + term.offset_e2 * fr.e2)
        if term.is_tilted:
            u = unit_from_tilt(fr.axis, fr.e1, fr.e2, term.tilt_deg,
                               term.azimuth_deg)
            v1 = fr.e1 - float(fr.e1 @ u) * u
            n = float(np.linalg.norm(v1))
            v1 = v1 / n if n > 1e-9 else fr.e2
        else:
            u, v1 = fr.axis, fr.e1
        v2 = np.cross(u, v1)
        return origin, u, v1, v2

    def term_center(self, term):
        """World position of a sphere term's centre."""
        fr = self.frame
        o, u, _v1, _v2 = self.term_frame(term)
        if term.center_mode == "group" and term.center_indices:
            return (self.st.center_of_mass(np.asarray(term.center_indices) - 1)
                    + term.offset_axial * fr.axis + term.offset_e1 * fr.e1
                    + term.offset_e2 * fr.e2)
        return o + term.center_z * u

    def revolve_about(self, base, u, v1, v2, zline, rline,
                      n_theta: int = 96):
        """Surface of revolution about an arbitrary axis."""
        zline = np.asarray(zline, dtype=np.float64)
        rline = np.asarray(rline, dtype=np.float64)
        rline = np.where(np.isfinite(rline), rline, 0.0)
        rline = np.where(rline > 0.0, rline, 0.0)
        theta = np.linspace(0.0, 2.0 * np.pi, n_theta, endpoint=False)
        ZZ, TT = np.meshgrid(zline, theta, indexing="ij")
        RR = np.repeat(rline[:, None], n_theta, axis=1)
        pts = (base + ZZ.ravel()[:, None] * u
               + (RR.ravel() * np.cos(TT.ravel()))[:, None] * v1
               + (RR.ravel() * np.sin(TT.ravel()))[:, None] * v2)
        nz = len(zline)
        faces = []
        for i in range(nz - 1):
            for j in range(n_theta):
                j2 = (j + 1) % n_theta
                a = i * n_theta + j
                b = i * n_theta + j2
                c = (i + 1) * n_theta + j2
                d = (i + 1) * n_theta + j
                faces.append((a, b, c))
                faces.append((a, c, d))
        return pts, np.asarray(faces, dtype=np.int64)

    def disc_about(self, base, u, v1, v2, z: float, radius: float,
                   n_theta: int = 96):
        theta = np.linspace(0.0, 2.0 * np.pi, n_theta, endpoint=False)
        r = max(float(radius), 1e-6)
        rim = (base + z * u + (r * np.cos(theta))[:, None] * v1
               + (r * np.sin(theta))[:, None] * v2)
        pts = np.vstack([(base + z * u)[None, :], rim])
        faces = [(0, 1 + j, 1 + (j + 1) % n_theta) for j in range(n_theta)]
        return pts, np.asarray(faces, dtype=np.int64)

    def term_surface_points(self, term, n_z: int = 24, n_theta: int = 16):
        """A cloud on the shape's own boundary, wherever it is pointing."""
        o, u, v1, v2 = self.term_frame(term)
        if term.kind == "sphere":
            c = self.term_center(term)
            th = np.linspace(0, 2 * np.pi, n_theta, endpoint=False)
            ph = np.linspace(0, np.pi, max(3, n_z // 2))
            P, T = np.meshgrid(ph, th, indexing="ij")
            return (c + term.radius * np.stack([
                (np.sin(P) * np.cos(T)).ravel(),
                (np.sin(P) * np.sin(T)).ravel(),
                np.cos(P).ravel()], axis=1))
        zs = np.linspace(term.z_lo, term.z_hi, n_z)
        if term.kind == "slab":
            r = np.full_like(zs, self.best_room(0.0))
        else:
            r = self.term_profile(term, zs)
            if r is None:
                return np.zeros((0, 3))
            r = np.where(np.isfinite(r), r, 0.0)
            if term.clamp_radius and term.radius_cap > 0:
                r = np.minimum(r, term.radius_cap)
        pts, _f = self.revolve_about(o, u, v1, v2, zs, r, n_theta)
        return pts

    def boundary_mesh(self, n_theta: int = 96, n_z: int = 2,
                      radial_offset: float = 0.0):
        """Triangulated surface of revolution of ``r_allowed(z) + offset``.

        Returns (points (N,3), faces (M,3)) with a kink inserted at z_cc.
        """
        sp = self.spec
        if sp.source_adapter:
            # Custom profiles need samples between their endpoints as well.
            n_z = max(n_z, 64)
        zs = [sp.z_min]
        if sp.z_min < sp.z_cc < sp.z_max:
            zs.append(sp.z_cc)
        zs.append(sp.z_max)
        # densify each straight segment (cheap, keeps the kink sharp)
        dense: list[float] = []
        for a, b in zip(zs[:-1], zs[1:]):
            seg = np.linspace(a, b, max(2, n_z))
            dense.extend(seg[:-1].tolist())
        dense.append(zs[-1])
        zline = np.asarray(dense, dtype=np.float64)
        rline = self.spec.r_allowed(zline) + radial_offset
        theta = np.linspace(0.0, 2.0 * np.pi, n_theta, endpoint=False)
        ZZ, TT = np.meshgrid(zline, theta, indexing="ij")
        RR = np.repeat(rline[:, None], n_theta, axis=1)
        pts = self.frame.cyl_to_world(ZZ.ravel(), RR.ravel(), TT.ravel())
        nz = len(zline)
        faces = []
        for i in range(nz - 1):
            for j in range(n_theta):
                j2 = (j + 1) % n_theta
                a = i * n_theta + j
                b = i * n_theta + j2
                c = (i + 1) * n_theta + j2
                d = (i + 1) * n_theta + j
                faces.append((a, b, c))
                faces.append((a, c, d))
        return pts, np.asarray(faces, dtype=np.int64), zline, rline

    def cap_mesh(self, which: str, n_theta: int = 96, radial_offset: float = 0.0,
                 axial_offset: float = 0.0):
        """Disc closing the funnel at z_min ('low') or z_max ('high')."""
        sp = self.spec
        z = (sp.z_min - axial_offset) if which == "low" else (sp.z_max + axial_offset)
        r = float(sp.r_allowed(z)) + radial_offset
        theta = np.linspace(0.0, 2.0 * np.pi, n_theta, endpoint=False)
        rim = self.frame.cyl_to_world(np.full(n_theta, z), np.full(n_theta, r), theta)
        center = self.frame.cyl_to_world(np.array([z]), np.array([0.0]),
                                         np.array([0.0]))
        pts = np.vstack([center, rim])
        faces = [(0, 1 + j, 1 + (j + 1) % n_theta) for j in range(n_theta)]
        return pts, np.asarray(faces, dtype=np.int64)

    def axis_polyline(self, pad_low: float = 0.0, pad_high: float = 0.0):
        sp = self.spec
        a = self.frame.cyl_to_world(np.array([sp.z_min - pad_low]),
                                    np.array([0.0]), np.array([0.0]))[0]
        b = self.frame.cyl_to_world(np.array([sp.z_max + pad_high]),
                                    np.array([0.0]), np.array([0.0]))[0]
        return a, b

    def field_bounds(self, pad: float = 4.0) -> tuple[float, float, float]:
        """Local-frame extent (z_lo, z_hi, r_out) covering every restraint."""
        sp = self.spec
        zs: list[float] = []
        rs: list[float] = []
        if sp.funnel_enabled:
            zs += [sp.z_min, sp.z_max]
            rs += [sp.r_base(), sp.r_cyl]
        ctx = None
        for t in sp.active_terms:
            if t.kind in ("lathe", "slab"):
                zs += [t.z_lo, t.z_hi]
            if t.kind == "lathe":
                if ctx is None:
                    ctx = self.field_ctx(z=np.array([0.0]),
                                         rho=np.array([0.0]))
                grid = np.linspace(t.z_lo, t.z_hi, 60)
                r = ctx.profile_radius(t, z=grid)
                if np.any(np.isfinite(r)):
                    rs.append(float(np.nanmax(r)))
            if t.kind == "sphere":
                if t.center_mode == "group" and t.center_indices:
                    com = self.st.center_of_mass(
                        np.asarray(t.center_indices) - 1)
                    cz, crho = self.cv(com)
                    zs += [float(cz) - t.radius, float(cz) + t.radius]
                    rs.append(float(crho) + t.radius)
                else:
                    zs += [t.center_z - t.radius, t.center_z + t.radius]
                    rs.append(t.radius)
            if t.is_tilted or t.is_off_axis:
                # a shape that leans or sits off the axis reaches further in
                # both coordinates than its own profile suggests
                pts = self.term_surface_points(t, n_z=12, n_theta=10)
                if len(pts):
                    tz, trho = self.cv(pts)
                    zs += [float(np.min(tz)), float(np.max(tz))]
                    rs.append(float(np.max(trho)))
            if t.region.enabled:
                zs += [t.region.z_lo, t.region.z_hi]
        z0, rho0 = self.cv_of_ligand()
        zs.append(z0)
        rs.append(rho0)
        if not zs:
            zs = [-10.0, 10.0]
        if not rs:
            rs = [10.0]
        r_out = float(max(rs)) + pad
        if not np.isfinite(r_out):
            r_out = 40.0
        # a runaway profile must not turn into a gigantic sampling grid
        r_out = float(min(r_out, 250.0))
        return (float(min(zs)) - pad, float(max(zs)) + pad, r_out)

    def sample_field_grid(self, n_axial: int = 72, n_radial: int = 56,
                          pad: float = 4.0, term=None,
                          apply_region: bool = True):
        """Sample the whole restraint on a regular grid of the funnel frame.

        Returns ``(xs, ys, zs, values)`` in local coordinates, where x and y
        span the perpendicular plane and z runs along the axis.
        """
        z_lo, z_hi, r_out = self.field_bounds(pad)
        fr = self.frame
        xs = np.linspace(-r_out, r_out, n_radial)
        ys = np.linspace(-r_out, r_out, n_radial)
        zs = np.linspace(z_lo, z_hi, n_axial)
        X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
        pts = (fr.origin
               + X.ravel()[:, None] * fr.e1
               + Y.ravel()[:, None] * fr.e2
               + Z.ravel()[:, None] * fr.axis)
        if term is None:
            v = np.asarray(self.total_field(points=pts), dtype=np.float64)
        else:
            ctx = self.field_ctx(points=pts)
            v = np.asarray(term.energy(ctx, apply_region=apply_region),
                           dtype=np.float64)
        v = np.where(np.isfinite(v), v, 0.0)
        return xs, ys, zs, v.reshape(X.shape)

    def local_to_world_matrix(self):
        """4x4 matrix mapping (e1, e2, axis) local coordinates to the world."""
        fr = self.frame
        m = np.eye(4)
        m[:3, 0] = fr.e1
        m[:3, 1] = fr.e2
        m[:3, 2] = fr.axis
        m[:3, 3] = fr.origin
        return m

    def term_profile(self, term, zs):
        """r(z) of a lathe/sphere term's boundary, or None."""
        ctx = self.field_ctx(z=np.asarray(zs, dtype=np.float64),
                             rho=np.zeros_like(np.asarray(zs, dtype=float)))
        return term.boundary_radius(ctx, np.asarray(zs, dtype=np.float64))

    def sample_volume(self, n_axial: int = 120, n_radial: int = 64,
                      v_max: float = 40.0, margin: float = 6.0):
        """Grid the wall potential in the funnel-local frame."""
        """Sample the wall potential on a cylindrical-aligned regular grid.

        Returns (origin, spacing, dims, values) in world axes suitable for a
        vtkImageData placed with an oriented transform, plus the transform.
        """
        sp = self.spec
        r_out = sp.r_base() + self.spec.radial_offset_for(v_max) + margin
        z_lo = sp.z_min - self.spec.axial_offset_for(v_max) - margin
        z_hi = sp.z_max + self.spec.axial_offset_for(v_max) + margin
        zs = np.linspace(z_lo, z_hi, n_axial)
        xs = np.linspace(-r_out, r_out, n_radial)
        ys = np.linspace(-r_out, r_out, n_radial)
        # values in the funnel-local frame: rho = sqrt(x^2 + y^2)
        X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
        RHO = np.sqrt(X * X + Y * Y + EPS_RHO)
        if sp.source_adapter:
            fr = self.frame
            points = (fr.origin + X.ravel()[:, None] * fr.e1
                      + Y.ravel()[:, None] * fr.e2
                      + Z.ravel()[:, None] * fr.axis)
            V = self.total_field(points=points).reshape(X.shape)
        else:
            V = sp.wall_potential(Z, RHO)
        return xs, ys, zs, V

    # -- diagnostics -------------------------------------------------
    def diagnostic_distances(self) -> list[tuple[str, float]]:
        out = []
        fr = self.frame
        for d in self.spec.diagnostics:
            if not d.indices:
                out.append((d.label, float("nan")))
                continue
            com = self.st.center_of_mass(np.asarray(d.indices) - 1)
            v = self.st.min_image(fr.lig_com - com)
            out.append((d.label, float(np.linalg.norm(v))))
        return out

    def atoms_in_funnel(self, mask=None) -> np.ndarray:
        """0-based indices of atoms inside the allowed funnel volume."""
        st = self.st
        idx = np.where(mask)[0] if mask is not None else np.arange(st.n_atoms)
        if idx.size == 0:
            return idx
        z, rho = self.cv(st.xyz[idx])
        sp = self.spec
        ok = (z >= sp.z_min) & (z <= sp.z_max) & (rho <= sp.r_allowed(z))
        return idx[ok]

    def envelope_profile(self, n: int = 64):
        """(z, r_max) of everything that restrains the ligand.

        Works whether or not the built-in funnel is on: the envelope is the
        widest boundary any active term defines at each z.
        """
        sp = self.spec
        z_lo, z_hi, r_out = self.field_bounds(pad=0.0)
        zs = np.linspace(z_lo, z_hi, n)
        r = np.zeros_like(zs)
        have = False
        if sp.funnel_enabled:
            inside = (zs >= sp.z_min) & (zs <= sp.z_max)
            r = np.where(inside, sp.r_allowed(zs), r)
            have = True
        for t in sp.active_terms:
            rr = self.term_profile(t, zs)
            if rr is None:
                continue
            rr = np.where(np.isfinite(rr), rr, 0.0)
            if t.kind == "lathe":
                rr = np.where((zs >= t.z_lo) & (zs <= t.z_hi), rr, 0.0)
            r = np.maximum(r, rr)
            have = True
        if not have:
            return zs, np.full_like(zs, np.nan)
        return zs, r

    def clearance_points(self, include_terms: bool = True) -> np.ndarray:
        """Every point the restraint actually occupies, in world coordinates.

        The funnel envelope is revolved about the biased axis; each shape is
        sampled in *its own* frame, so a sphere parked off to one side is
        measured where it really is instead of being smeared into a ring
        around the funnel axis.
        """
        sp = self.spec
        fr = self.frame
        clouds: list[np.ndarray] = []
        if sp.funnel_enabled and not self.funnel_is_degenerate():
            zs = np.linspace(sp.z_min, sp.z_max, 48)
            theta = np.linspace(0.0, 2.0 * np.pi, 32, endpoint=False)
            ZZ, TT = np.meshgrid(zs, theta, indexing="ij")
            RR = np.repeat(sp.r_allowed(zs)[:, None], len(theta), axis=1)
            clouds.append(fr.cyl_to_world(ZZ.ravel(), RR.ravel(), TT.ravel()))
        if include_terms:
            for t in sp.active_terms:
                if t.kind == "slab":
                    o, u, _v1, _v2 = self.term_frame(t)
                    clouds.append(np.array([o + t.z_lo * u, o + t.z_hi * u]))
                    continue
                pts = self.term_surface_points(t)
                if len(pts):
                    clouds.append(pts)
        if not clouds:
            return np.zeros((0, 3))
        return np.vstack(clouds)

    def funnel_clearance(self, include_terms: bool = True
                         ) -> tuple[float, np.ndarray]:
        """Smallest distance from the restraint to a box face, and where.

        Negative means something is outside the cell.  It agrees with
        :meth:`outside_box` by construction: both read the same geometry, so
        a shape that sits off the funnel axis is never reported as sticking
        out just because a ring swept around the axis would.
        """
        pts = self.clearance_points(include_terms)
        if not len(pts):
            return float("nan"), self.frame.origin
        centre, normals, halves = self.box_planes()
        d = np.stack([h - np.abs((pts - centre) @ n)
                      for n, h in zip(normals, halves)], axis=1)
        worst = d.min(axis=1)
        k = int(np.argmin(worst))
        return float(worst[k]), pts[k]

    def _check_confinement(self, add) -> None:
        """Is the biased particle actually enclosed by the restraints?"""
        sp = self.spec
        fr = self.frame
        span = max(40.0, 2.0 * (sp.z_max - sp.z_min))
        rng = np.random.default_rng(4)
        zs = rng.uniform(sp.z_min - span, sp.z_max + span, 4000)
        rr = rng.uniform(0.0, sp.r_base() + span, 4000)
        ph = rng.uniform(0.0, 2 * np.pi, 4000)
        pts = fr.cyl_to_world(zs, rr, ph)
        v = np.asarray(self.total_field(points=pts))
        far = (np.abs(zs - 0.5 * (sp.z_min + sp.z_max))
               > 0.5 * (sp.z_max - sp.z_min) + 10.0) | \
              (rr > sp.r_base() + 10.0)
        leak = far & (v < 1e-6)
        if leak.any():
            frac = 100.0 * leak.sum() / max(1, far.sum())
            add(Issue("warning", "term",
                      f"The restraints leave {frac:.0f}% of the far field at "
                      "zero energy",
                      "The ligand could drift out there without ever feeling "
                      "a wall. Close the volume, or add a slab or lathe cap."))

    # -- validation --------------------------------------------------
    def validate(self, cutoff_radius: float = 9.0,
                 production_ps: float | None = None,
                 cfg_temperature: float | None = None) -> list[Issue]:
        st, sp = self.st, self.spec
        out: list[Issue] = []
        add = out.append

        # --- selections
        groups = [(sp.lig, "Ligand"), (sp.site, "SITE"), (sp.core, "CORE")]
        if sp.has_frame_ref:
            groups.append((sp.frame_ref, "Frame reference"))
        for sel, what in groups:
            if sel.n == 0:
                add(Issue("error", "selection", f"{what} selection is empty",
                          f"`{sel.var} = atomsel(...)` needs at least one atom."))
                continue
            bad = [i for i in sel.indices if i < 1 or i > st.n_atoms]
            if bad:
                add(Issue("error", "selection",
                          f"{what} has {len(bad)} out-of-range atom indices",
                          f"System has {st.n_atoms} atoms; first offender: {bad[0]}."))
            idx0 = sel.idx0()
            idx0 = idx0[(idx0 >= 0) & (idx0 < st.n_atoms)]
            nh = int((st.anum[idx0] == 1).sum())
            if nh:
                add(Issue("warning", "selection",
                          f"{what} contains {nh} hydrogen atom(s)",
                          "Funnel CVs are normally defined on heavy atoms only so "
                          "that the COM is insensitive to constrained X-H motion."))
            nw = int(st.mask("water")[idx0].sum())
            if nw:
                add(Issue("error", "selection",
                          f"{what} contains {nw} solvent atom(s)",
                          "Water molecules diffuse; they must not define the frame."))
            if len(set(sel.indices)) != len(sel.indices):
                add(Issue("warning", "selection",
                          f"{what} lists duplicate atom indices",
                          "Duplicates silently re-weight the centre of mass."))

        # --- ligand sanity
        if sp.lig.n:
            idx0 = sp.lig.idx0()
            idx0 = idx0[(idx0 >= 0) & (idx0 < st.n_atoms)]
            res = {st.residue_of_atom(int(i)).long_label for i in idx0
                   if st.residue_of_atom(int(i))}
            if len(res) > 1:
                add(Issue("info", "selection",
                          f"Ligand spans {len(res)} residues",
                          ", ".join(sorted(res))))

        # --- frame quality
        fr = self.frame
        if fr.axis_len < 5.0:
            add(Issue("warning", "frame",
                      f"SITE-CORE axis is short ({fr.axis_len:.2f} A)",
                      "A short axis makes the funnel direction noisy; pick core "
                      "atoms deeper in the protein body."))
        # site/core must be rigid: report their radius of gyration
        for sel, what in ((sp.site, "SITE"), (sp.core, "CORE")):
            if sel.n < 2:
                continue
            p = st.xyz[sel.idx0()]
            rg = float(np.sqrt(((p - p.mean(axis=0)) ** 2).sum(axis=1).mean()))
            if rg > 20.0:
                add(Issue("warning", "frame",
                          f"{what} atoms are spread over {rg:.1f} A (Rg)",
                          "Widely spread frame groups drift relative to each other."))

        # --- geometry
        if not sp.funnel_enabled and not sp.active_terms:
            add(Issue("error", "geometry",
                      "No restraint is defined at all",
                      "Either enable the built-in funnel or add at least one "
                      "shape term; without a wall the ligand simply diffuses "
                      "away and the metadynamics bias never converges."))
        if sp.funnel_enabled and sp.z_max <= sp.z_min:
            add(Issue("error", "geometry", "z_max must be larger than z_min",
                      f"z_min={sp.z_min}, z_max={sp.z_max}"))
        if sp.funnel_enabled and not (sp.z_min <= sp.z_cc <= sp.z_max):
            add(Issue("warning", "geometry",
                      "z_cc lies outside [z_min, z_max]",
                      "The cone/cylinder junction is then never sampled; the "
                      "funnel degenerates to a pure cone or pure cylinder."))
        if sp.funnel_enabled and sp.r_cyl <= 0:
            add(Issue("error", "geometry", "r_cyl must be positive", ""))
        if sp.funnel_enabled and sp.cone_slope < 0:
            add(Issue("error", "geometry", "cone_slope must be >= 0",
                      "A negative slope inverts the funnel and traps the ligand."))
        if sp.funnel_enabled and sp.k_rad <= 0:
            add(Issue("error", "force", "k_rad must be positive", ""))
        if sp.funnel_enabled and sp.k_z <= 0:
            add(Issue("error", "force", "k_z must be positive", ""))

        # --- start state
        z0, rho0 = self.cv_of_ligand()
        r0 = float(sp.r_allowed(z0))
        v0 = float(self.total_at_ligand())
        if sp.funnel_enabled and not (sp.z_min < z0 < sp.z_max):
            add(Issue("error", "start",
                      f"Ligand starts outside the axial walls (z0 = {z0:.3f} A)",
                      f"Allowed range is [{sp.z_min}, {sp.z_max}]."))
        elif sp.funnel_enabled and min(z0 - sp.z_min, sp.z_max - z0) < 2.0:
            add(Issue("warning", "start",
                      f"Ligand starts {min(z0 - sp.z_min, sp.z_max - z0):.2f} A "
                      "from an axial wall",
                      "Give the bound state room, or the wall will do the work "
                      "of the bias."))
        if sp.funnel_enabled and rho0 > r0:
            add(Issue("error", "start",
                      f"Ligand starts outside the funnel wall "
                      f"(rho0 = {rho0:.3f} A > r_allowed = {r0:.3f} A)",
                      f"Starting bias would be {v0:.2f} kcal/mol; the run would "
                      "kick the ligand at t = 0."))
        elif v0 > 1e-6:
            add(Issue("warning", "start",
                      f"Non-zero starting restraint: {v0:.4f} kcal/mol", ""))
        else:
            add(Issue("ok", "start",
                      f"Ligand starts free: z0 = {z0:.3f} A, rho0 = {rho0:.3f} A, "
                      f"r_allowed = {r0:.2f} A, V = 0", ""))

        # --- body-fixed frame for off-axis placement
        if sp.uses_perp_frame() and not sp.has_frame_ref:
            offenders = [t.label or t.var for t in sp.active_terms
                         if t.offset_e1 or t.offset_e2]
            if sp.origin_lat1 or sp.origin_lat2:
                offenders.insert(0, "the funnel origin")
            add(Issue("error", "frame",
                      "Something is placed off the axis but there is no frame "
                      "reference group",
                      "e1 and e2 would be undefined in the potential. Pick a "
                      "reference group (Shapes tab) or set the sideways "
                      "offsets back to zero. Affected: "
                      + ", ".join(offenders)))
        elif sp.has_frame_ref:
            try:
                ref = st.center_of_mass(sp.frame_ref.idx0())
                v = st.min_image(ref - fr.core_com)
                perp = float(np.linalg.norm(v - float(v @ fr.axis) * fr.axis))
            except Exception:
                perp = 0.0
            if perp < 2.0:
                add(Issue("error", "frame",
                          f"The frame reference is only {perp:.2f} A off the "
                          "axis",
                          "e1 = the perpendicular part of CORE -> reference, "
                          "so a nearly parallel reference makes that direction "
                          "numerically meaningless. Pick a group well to the "
                          "side of the axis."))
            elif perp < 5.0:
                add(Issue("warning", "frame",
                          f"The frame reference is {perp:.2f} A off the axis",
                          "Sideways placement will be noisy; a group further "
                          "from the axis defines e1 more stiffly."))
            else:
                add(Issue("ok", "frame",
                          f"Body-fixed frame from {sp.frame_ref.n} atoms, "
                          f"{perp:.2f} A off the axis", ""))

        # --- names in the generated file have to stay unique
        seen_names: dict[str, str] = {}
        for owner, names in ([(f"diagnostic {d.label}",
                               (d.var, f"{d.var}_sel", d.dist_var))
                              for d in sp.diagnostics]
                             + [(f"term {t.label or t.var}",
                                 (t.var, t.energy_var)) for t in sp.terms]):
            for n in names:
                if n in RESERVED_NAMES:
                    add(Issue("error", "names",
                              f"{owner} uses the reserved name `{n}`",
                              "The potential already binds that name, and the "
                              "M-expression language allows a single "
                              "assignment per name."))
                elif n in seen_names:
                    add(Issue("error", "names",
                              f"`{n}` is used by both {seen_names[n]} and "
                              f"{owner}",
                              "Two assignments to one name make the potential "
                              "invalid; rename one of them."))
                else:
                    seen_names[n] = owner

        # --- extra shape terms
        from .terms import validate_term
        seen_vars: dict[str, str] = {}
        for t in sp.terms:
            if t.var in seen_vars:
                add(Issue("error", "term",
                          f"Two terms both use the variable `{t.var}`",
                          f"{seen_vars[t.var]} and {t.label}"))
            seen_vars[t.var] = t.label or t.var
            if t.enabled:
                out.extend(validate_term(t, sp, self, Issue))
        if sp.active_terms:
            add(Issue("info", "term",
                      f"{len(sp.active_terms)} extra shape term(s) active",
                      ", ".join(f"{t.label or t.var} ({t.kind})"
                                for t in sp.active_terms)))
            self._check_confinement(add)

        if sp.axis_is_tilted and not sp.has_frame_ref:
            add(Issue("error", "frame",
                      "The funnel axis is tilted but there is no frame "
                      "reference group",
                      "The tilt is written as a combination of the axis and "
                      "two body-fixed perpendiculars; without a reference "
                      "group those do not exist."))
        elif sp.axis_is_tilted:
            add(Issue("info", "frame",
                      f"The biased axis is tilted {sp.axis_tilt_deg:.2f}° away "
                      f"from CORE→SITE, towards {sp.axis_azimuth_deg:.1f}°",
                      "z is measured along the tilted direction, so the "
                      "collective variable itself changes with it."))

        # --- nothing may sit outside the supplied solvent box
        for msg in self.outside_box():
            add(Issue("error", "box", msg[0].upper() + msg[1:],
                      "A restraint outside the periodic cell is meaningless: "
                      "the ligand can never be there, and min_image would fold "
                      "the coordinate back anyway. Editing any geometry pulls "
                      "the shapes back in; this file was left as it was found."))

        # --- box / periodicity
        clear, pt = self.funnel_clearance()
        if clear < 0:
            add(Issue("error", "box",
                      f"Funnel envelope leaves the periodic box by {-clear:.2f} A",
                      "min_image() would fold the CV and the wall would act on "
                      "the wrong image."))
        elif clear < cutoff_radius:
            add(Issue("warning", "box",
                      f"Funnel envelope comes within {clear:.2f} A of a box face",
                      f"Less than the {cutoff_radius:.1f} A non-bonded cutoff: the "
                      "dissociated ligand can feel the periodic image of the "
                      "protein."))
        else:
            add(Issue("ok", "box",
                      f"Funnel envelope clears every box face by {clear:.2f} A "
                      f"(cutoff {cutoff_radius:.1f} A)", ""))
        half = np.linalg.norm(st.box, axis=1) / 2.0
        if fr.axis_len > float(half.min()):
            add(Issue("warning", "box",
                      "SITE-CORE axis is longer than half the box edge",
                      "min_image() on the axis can flip sign during the run."))

        # --- exit channel must be solvent
        solute_heavy = st.mask("heavy") & ~st.mask("water")
        idx = np.where(solute_heavy)[0]
        z, rho = self.cv(st.xyz[idx])
        in_cyl = (z > sp.z_cc) & (z <= sp.z_max) & (rho <= sp.r_cyl)
        n_cyl = int(in_cyl.sum())
        if n_cyl:
            labels = sorted({(st.residue_of_atom(int(i)).long_label)
                             for i in idx[in_cyl]})[:8]
            add(Issue("warning", "channel",
                      f"{n_cyl} solute heavy atom(s) sit inside the bulk cylinder",
                      "The unbinding channel should be pure solvent beyond z_cc. "
                      "Offending residues: " + ", ".join(labels)))
        else:
            add(Issue("ok", "channel",
                      "Bulk cylinder (z > z_cc) is free of solute atoms", ""))
        n_fun = int(((z >= sp.z_min) & (z <= sp.z_max) &
                     (rho <= sp.r_allowed(z))).sum())
        add(Issue("info", "channel",
                  f"{n_fun} solute heavy atoms lie inside the funnel volume "
                  f"({sp.funnel_volume() / 1000.0:.1f} nm^3)",
                  "Mostly the pocket walls at the wide end - expected."))

        # --- well tempered parameters
        g = sp.gamma_from_ktemp()
        if sp.ktemp <= 0:
            add(Issue("error", "wt", "kTemp must be positive",
                      "kTemp = (gamma - 1) kB T"))
        elif abs(g - sp.gamma) > 1e-3:
            add(Issue("warning", "wt",
                      f"kTemp literal implies gamma = {g:.4f}, spec says "
                      f"{sp.gamma:.4f}",
                      "Recompute kTemp from gamma, or update gamma."))
        if cfg_temperature is not None and abs(cfg_temperature - sp.temperature) > 1e-6:
            add(Issue("error", "wt",
                      f"Bias temperature {sp.temperature:.2f} K does not match the "
                      f".cfg ensemble temperature {cfg_temperature:.2f} K",
                      "kTemp = (gamma - 1) kB T must use the simulated T, or the "
                      "reweighting factor is wrong."))
        if sp.h0 <= 0:
            add(Issue("error", "wt", "Hill height h0 must be positive", ""))
        if sp.sigma_z <= 0:
            add(Issue("error", "wt", "sigma_z must be positive", ""))
        if sp.sigma_z > 2.0:
            add(Issue("warning", "wt",
                      f"sigma_z = {sp.sigma_z:.2f} A is wide",
                      "Wide kernels smear the free-energy profile; 0.2-0.5 A is "
                      "typical for a distance-like CV."))
        span = sp.z_max - sp.z_min
        if sp.sigma_z > 0 and span / sp.sigma_z < 20:
            add(Issue("warning", "wt",
                      f"Only {span / sp.sigma_z:.0f} kernel widths span the CV "
                      "range", "Consider a smaller sigma_z."))
        if sp.dimension == 2 and sp.sigma_rho <= 0:
            add(Issue("error", "wt", "sigma_rho must be positive in 2-D", ""))
        if sp.initial_kernels.strip():
            add(Issue("info", "wt",
                      f"Restarting from kernel file {sp.initial_kernels!r}",
                      "That file has to exist in the job directory when "
                      "multisim starts, or the run aborts."))
        if sp.kernel_cutoff <= 3:
            add(Issue("warning", "wt",
                      f"Kernel cutoff {sp.kernel_cutoff} is small",
                      "Truncating Gaussians below ~4 sigma leaves visible steps "
                      "in the bias."))
        if production_ps is not None:
            if sp.meta_first >= production_ps:
                add(Issue("error", "wt",
                          "First hill is deposited after the run ends",
                          f"first = {sp.meta_first} ps, production = "
                          f"{production_ps} ps"))
            else:
                n = sp.hills_for_time(production_ps)
                add(Issue("info", "wt",
                          f"{n} hills over {production_ps / 1000:.1f} ns "
                          f"({1000.0 / sp.meta_interval:.0f} hills/ns, "
                          f"h0 = {sp.h0} kcal/mol)",
                          f"Untempered deposition rate "
                          f"{sp.h0 * 1000.0 / sp.meta_interval:.2f} kcal/mol/ns."))
            if sp.cv_interval > 0 and production_ps / sp.cv_interval > 2e6:
                add(Issue("warning", "wt",
                          "CV output interval writes more than 2e6 records",
                          "The .cvseq file will be very large."))
        return out
