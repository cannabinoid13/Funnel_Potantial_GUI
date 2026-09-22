"""Self-tests for the FunnelForge core.

Run with the reference job that ships with the machine::

    python -m pytest tests -q
    python tests/test_funnelforge.py           # standalone, prints a report

Everything that touches Desmond semantics is checked twice: once through the
interface model and once by re-evaluating the generated M-expression with the
independent interpreter in ``funnelforge.core.mexpr``.
"""

from __future__ import annotations

import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from funnelforge.core.cms import Structure
from funnelforge.core.potfile import (parse_pot, emit_pot,
                                      parse_asl_atoms, fmt_number)
from funnelforge.core.funnel import FunnelModel, FunnelSpec, Diagnostic, KB_KCAL
from funnelforge.core.mexpr import evaluate_pot, MExprError, lex, Parser
from funnelforge.core.asl import parse_selection
from funnelforge.core.project import Job
from funnelforge.core.blocktext import BlockText
from funnelforge.core.jobfiles import CfgFile, MsjFile, ShFile, discover_bundle

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture_paths import designer_job              # noqa: E402

#: The funnel designer models one template, so its suite needs a job in that
#: template.  It is built from the repository's own structure the first time
#: it is asked for, which also exercises the "import a bare .cms and fit a
#: funnel" path end to end.  Nothing here is tied to a particular molecule:
#: every expectation below is derived from the fixture rather than written
#: as a constant.
try:
    JOB = designer_job()
except Exception as _exc:               # pragma: no cover - reported below
    JOB = ""
    _BUILD_ERROR = _exc
else:
    _BUILD_ERROR = None
HAVE_JOB = bool(JOB) and os.path.exists(JOB)

_cache: dict = {}


def load_job() -> Job:
    if "job" not in _cache:
        job = Job()
        job.import_bundle(JOB)
        _cache["job"] = job
    return _cache["job"]


# ---------------------------------------------------------------- structure
def test_structure_reads_box_and_atoms():
    st = load_job().structure
    assert st.n_atoms > 1000
    assert st.xyz.shape == (st.n_atoms, 3)
    assert np.isfinite(st.xyz).all()
    assert st.is_orthorhombic
    L = np.diag(st.box)
    assert (L > 10.0).all() and np.isfinite(L).all()
    assert 0 < len(st.bonds) < 4 * st.n_atoms
    assert all(0 <= a < st.n_atoms and 0 <= b < st.n_atoms
               for a, b in st.bonds[:200])
    # component cts concatenate into the full system
    assert sum(c.natoms for c in st.cts) == st.n_atoms
    # force-field masses, not periodic-table averages
    assert abs(st.mass[0] - 14.0067) < 1e-6
    assert abs(st.mass[1] - 12.01115) < 1e-6


def test_structure_residue_annotation():
    """Residue labelling is checked against the structure, not a constant."""
    st = load_job().structure
    sp = load_job().spec
    # the frame groups the fixture actually uses, whatever they select
    for group in (sp.site, sp.core):
        idx = np.asarray(group.idx0())
        sig = st.residue_signature(idx)
        assert sig, group.indices[:4]
        # one label per distinct residue the group touches, in order
        residues = []
        for i in idx:
            r = st.residue_of_atom(int(i))
            if r.long_label not in residues:
                residues.append(r.long_label)
        assert len(sig) == len(residues)
        for label, long_label in zip(sig, residues):
            # the signature is "Leu110"-style; long_label is "A:LEU110"-style
            assert label.upper() == long_label.split(":")[-1].upper()
    # describe_atom names the atom it is given
    probe = int(np.asarray(sp.lig.idx0())[0])
    words = st.describe_atom(probe).split()
    assert len(words) >= 3 and words[2]


# ---------------------------------------------------------------- .pot I/O
def test_pot_round_trip_is_byte_identical():
    src = open(JOB).read()
    spec, rep = parse_pot(src, JOB)
    assert rep.ok, rep.errors
    assert not rep.unknown_statements, rep.unknown_statements
    assert emit_pot(spec, None) == src
    assert emit_pot(spec, load_job().structure) == src


def test_pot_parsed_values():
    text = open(JOB).read()
    spec, _ = parse_pot(text)
    assert spec.dimension == 1
    # every group is non-empty and matches the atomsel it was read from
    for name, group in (("lig", spec.lig), ("site", spec.site),
                        ("core", spec.core)):
        assert group.n > 0, name
        assert f'{name} = atomsel("atom. ' in text
        first = str(group.indices[0])
        assert f'{name} = atomsel("atom. {first},' in text or \
               f'{name} = atomsel("atom. {first}"' in text
    assert spec.origin_mode in ("fraction", "absolute")
    # the geometry is a real funnel, and each literal appears in the file
    assert spec.z_min < spec.z_cc <= spec.z_max
    assert spec.r_cyl > 0 and spec.k_rad > 0 and spec.k_z > 0
    for value in (spec.z_cc, spec.r_cyl, spec.k_rad, spec.z_min, spec.z_max):
        assert fmt_number(value) in text, value
    # well-tempering is self-consistent
    assert spec.h0 > 0 and spec.sigma_z > 0
    assert spec.gamma_from_ktemp() > 1.0
    # the printed diagnostics are exactly the ones declared
    assert len(spec.prints) >= 1
    for d in spec.diagnostics:
        assert f'print("{d.label}' in text or d.var in text


def test_pot_literals_survive_unrelated_edits():
    src = open(JOB).read()
    spec, _ = parse_pot(src)
    # the exact spellings this file uses, whatever they are
    spelled = {}
    for name in ("cone_slope", "ktemp", "h0", "sigma_z", "r_cyl", "k_rad",
                 "z_min", "k_z"):
        for line in src.splitlines():
            if line.strip().startswith(f"{name} = "):
                spelled[name] = line.strip()
                break
    assert len(spelled) >= 6, sorted(spelled)
    origin_line = next(l.strip() for l in src.splitlines()
                       if l.strip().startswith("origin = "))

    spec.z_max = 30.0                      # one edit
    spec.literals.pop("z_max", None)
    text = emit_pot(spec, None)
    assert "z_max = 30.0;" in text
    # every other literal keeps its exact imported spelling, digit for digit
    for name, line in spelled.items():
        assert line in text, (name, line)
    assert origin_line in text


def test_asl_helpers():
    assert parse_asl_atoms("atom. 1,2,5-7") == [1, 2, 5, 6, 7]
    assert parse_asl_atoms(" 3 ") == [3]


# ---------------------------------------------------------------- geometry
def test_cv_matches_hand_computation():
    job = load_job()
    st, sp, m = job.structure, job.spec, job.model
    lc = st.center_of_mass(sp.lig.idx0())
    sc = st.center_of_mass(sp.site.idx0())
    cc = st.center_of_mass(sp.core.idx0())
    axis_raw = st.min_image(sc - cc)
    axis = axis_raw / np.linalg.norm(axis_raw)
    origin = cc + sp.origin_frac * axis_raw
    d = st.min_image(lc - origin)
    z = float(d @ axis)
    rho = float(np.sqrt(np.sum((d - z * axis) ** 2) + 1e-12))
    z_m, rho_m = m.cv_of_ligand()
    assert abs(z - z_m) < 1e-12 and abs(rho - rho_m) < 1e-12
    # and the same two numbers come back out of the emitted potential when an
    # independent interpreter evaluates it: three routes, one answer
    res = evaluate_pot(load_job().pot_text(), st)
    assert abs(res.scalar("z") - z_m) < 1e-9
    assert abs(res.scalar("rho") - rho_m) < 1e-9
    # the ligand starts inside the funnel it was fitted to
    assert rho_m < sp.r_allowed(z_m)


def test_start_state_is_unbiased():
    job = load_job()
    z0, rho0 = job.model.cv_of_ligand()
    assert rho0 < job.spec.r_allowed(z0)
    assert job.spec.wall_potential(z0, rho0) == 0.0


def test_wall_shape_and_energies():
    sp = load_job().spec
    assert abs(float(sp.r_allowed(sp.z_cc)) - sp.r_cyl) < 1e-12
    assert abs(float(sp.r_allowed(sp.z_cc + 5)) - sp.r_cyl) < 1e-12
    assert abs(sp.r_base() - (sp.r_cyl + (sp.z_cc - sp.z_min) *
                              sp.cone_slope)) < 1e-12
    # 1 A outside the wall must cost 0.5*k
    r1 = float(sp.r_allowed(0.0)) + 1.0
    assert abs(float(sp.v_rad(0.0, r1)) - 0.5 * sp.k_rad) < 1e-9
    assert abs(float(sp.v_zwall(sp.z_max + 2.0)) - 2.0 * sp.k_z) < 1e-9
    assert float(sp.v_zwall(0.5 * (sp.z_min + sp.z_max))) == 0.0
    assert abs(sp.radial_offset_for(0.5 * sp.k_rad) - 1.0) < 1e-9


def test_well_tempered_arithmetic():
    sp = load_job().spec
    assert abs((sp.gamma - 1) * KB_KCAL * sp.temperature - sp.ktemp) < 1e-8
    sp2 = sp.copy()
    sp2.gamma = 10.0
    sp2.sync_ktemp()
    assert abs(sp2.ktemp - 9 * KB_KCAL * 310.0) < 1e-12
    assert abs(sp2.gamma_from_ktemp() - 10.0) < 1e-9
    assert sp.hills_for_time(500000.0) == int((500000 - 100) / 2) + 1


def test_box_clearance_and_funnel_volume():
    m = load_job().model
    clear, pt = m.funnel_clearance()
    # the reported clearance is a real distance from a real point on the
    # restraint to the nearest box face, so recompute it by hand
    centre, normals, halves = m.box_planes()
    by_hand = min(h - abs(float((pt - centre) @ n))
                  for n, h in zip(normals, halves))
    assert abs(clear - by_hand) < 1e-9, (clear, by_hand)
    # and no sampled point of the restraint is closer than the reported value
    pts = m.clearance_points()
    worst = min(min(h - abs(float((q - centre) @ n))
                    for n, h in zip(normals, halves)) for q in pts[::37])
    assert worst >= clear - 1e-9
    assert clear > 0.0, "the fitted funnel must be inside its own box"
    sp = m.spec
    import math
    r0, r1 = sp.r_base(), sp.r_cyl
    h = sp.z_cc - sp.z_min
    expect = (math.pi * h * (r0 * r0 + r0 * r1 + r1 * r1) / 3.0
              + math.pi * r1 ** 2 * (sp.z_max - sp.z_cc))
    assert abs(sp.funnel_volume() - expect) < 1e-6


def test_exit_channel_is_solvent_only():
    job = load_job()
    st, m, sp = job.structure, job.model, job.spec
    idx = np.where(st.mask("heavy") & ~st.mask("water"))[0]
    z, rho = m.cv(st.xyz[idx])
    assert int(((z > sp.z_cc) & (z <= sp.z_max) & (rho <= sp.r_cyl)).sum()) == 0


# ---------------------------------------------------------------- evaluator
def test_evaluator_agrees_with_model():
    job = load_job()
    res = evaluate_pot(job.pot_text(), job.structure)
    z_m, rho_m = job.model.cv_of_ligand()
    assert abs(res.scalar("z") - z_m) < 1e-12
    assert abs(res.scalar("rho") - rho_m) < 1e-12
    assert abs(res.scalar("axis_len") - job.model.frame.axis_len) < 1e-12
    assert res.scalar("v_total") == 0.0
    assert abs(res.scalar("hill") - job.spec.h0) < 1e-15
    p = res.printed()
    # every declared diagnostic is printed, and each printed distance really
    # is the distance between that group and the ligand centre of mass
    st = job.structure
    lig_com = st.center_of_mass(job.spec.lig.idx0())
    assert job.spec.diagnostics, "the fixture should declare some distances"
    for d in job.spec.diagnostics:
        key = next(k for k in p if k.startswith(d.label))
        com = st.center_of_mass(np.asarray(d.indices) - 1)
        by_hand = float(np.linalg.norm(st.min_image(lig_com - com)))
        assert abs(p[key] - by_hand) < 1e-6, (key, p[key], by_hand)
    assert res.declares["declare_meta"]["dimension"] == 1
    assert res.meta_calls == 2


def test_evaluator_vectorised_field_matches():
    job = load_job()
    sp, m = job.spec, job.model
    rng = np.random.default_rng(7)
    fr = m.frame
    pts = fr.cyl_to_world(rng.uniform(sp.z_min - 10, sp.z_max + 10, 3000),
                          rng.uniform(0, sp.r_base() + 10, 3000),
                          rng.uniform(0, 2 * np.pi, 3000))
    res = evaluate_pot(job.pot_text(), job.structure, probe=pts)
    z, rho = m.cv(pts)
    assert np.abs(res.array("z") - z).max() < 1e-9
    assert np.abs(res.array("rho") - rho).max() < 1e-9
    assert np.abs(res.array("v_total") - sp.wall_potential(z, rho)).max() < 1e-8


def test_evaluator_language_basics():
    """The interpreter must match the language in the Desmond guide, ch. 11."""
    class FakeStructure:
        n_atoms = 3
        xyz = np.array([[0.0, 0.0, 0.0], [3.0, 0.0, 0.0], [0.0, 4.0, 0.0]])
        mass = np.array([1.0, 2.0, 4.0])
        def center_of_mass(self, idx):
            idx = np.asarray(idx, dtype=int)
            w = self.mass[idx]
            return (self.xyz[idx] * w[:, None]).sum(axis=0) / w.sum()
        def min_image(self, v):
            return np.asarray(v)
    fs = FakeStructure()

    def ev(src):
        return evaluate_pot(src, fs)

    # arithmetic, precedence, integer powers
    assert ev("a = 2.0; b = 3.0; c = a^2+b*2-1.0; c;").scalar("c") == 9.0
    # conditional: positive branch when the condition is > 0
    r = ev("x = if 1.0 then 5.0 else 7.0; y = if -1.0 then 5.0 else 7.0; x;")
    assert r.scalar("x") == 5.0 and r.scalar("y") == 7.0
    assert ev("z = if 0.0 then 1.0 else 2.0; z;").scalar("z") == 2.0
    # a scalar *is* a length-1 array, so norm(1.0) is legal
    assert ev("n = norm(1.0); n;").scalar("n") == 1.0
    # array(), length(), sum(), subscripts, binary threading
    r = ev("v = array(3.0,4.0,12.0); l = length(v); s = sum(v); "
           "n = norm(v); k = v[1]; t = v*2.0; u = sum(t); n;")
    assert r.scalar("l") == 3.0 and r.scalar("s") == 19.0
    assert abs(r.scalar("n") - 13.0) < 1e-12
    assert r.scalar("k") == 4.0 and r.scalar("u") == 38.0
    # dot and cross on real vectors
    r = ev("a = array(1.0,0.0,0.0); b = array(0.0,1.0,0.0); "
           "d = dot(a,b); c = cross(a,b); e = c[2]; d;")
    assert r.scalar("d") == 0.0 and r.scalar("e") == 1.0
    # Desmond-only helpers
    assert ev("s = sign(-3.0); s;").scalar("s") == -1.0
    assert ev("s = sign(0.0); s;").scalar("s") == 1.0
    assert abs(ev("p = pow(9.0,0.5); p;").scalar("p") - 3.0) < 1e-12
    assert abs(ev("m = mod(7.0,3.0); m;").scalar("m") - 1.0) < 1e-12
    assert abs(ev("g = gibbs_max(0.001,array(1.0,5.0)); g;").scalar("g")
               - 5.0) < 1e-2
    assert abs(ev("g = gibbs_min(0.001,array(1.0,5.0)); g;").scalar("g")
               - 1.0) < 1e-2
    # blocks introduce a scope and evaluate to their last expression
    r = ev("q = { r = 3.0; r*r; }; q;")
    assert r.scalar("q") == 9.0
    # series sums its body
    r = ev("s = series (i=0:4) i*i; s;")
    assert r.scalar("s") == 14.0
    # particle functions
    r = ev('p = atomsel("atom. 1,2,3"); c = center_of_mass(p); '
           'd = dist(p[1],p[0]); m = mass(p[2]); x = pos(p[1]); '
           'g = rad_gyration(p); d;')
    assert abs(r.scalar("d") - 3.0) < 1e-12
    assert r.scalar("m") == 4.0
    # single assignment is enforced
    for bad, why in (
            ("a = 1.0; a = 2.0; a;", "single assignment"),
            ("a = ;", "syntax"),
            ("a = 1.0", "missing semicolon"),
            ("a = min_image(1.0); a;", "min_image needs length 3"),
            ("a = abs(-1.0); a;", "abs does not exist in Desmond"),
            ("a = max(1.0,2.0); a;", "max does not exist in Desmond"),
            ("a = tan(1.0); a;", "tan does not exist in Desmond"),
            ("a = frobnicate(1.0); a;", "unknown function"),
            ("a = dot(array(1.0,2.0),array(1.0,2.0,3.0)); a;", "threading"),
            ("a = { b = 1.0; }; a;", "block must end with an expression"),
            ("a = sqrt(1.0,2.0); a;", "wrong argument count"),
    ):
        try:
            ev(bad)
        except MExprError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected ({why})")


def test_expression_static_checks():
    from funnelforge.core.mexpr import check_expression, NOT_IN_DESMOND
    known = {"z", "rho", "k"}
    assert check_expression("0.5*k*(rho-4.0)^2", known) == []
    assert check_expression("if z then z^2 else 0.0", known) == []
    # ^ is integer-power only in Desmond
    bad = check_expression("rho^2.5", known)
    assert bad and "pow(" in bad[0]
    bad = check_expression("rho^k", known)
    assert bad and "integer" in bad[0]
    assert check_expression("pow(rho,2.5)", known) == []
    # unknown names and non-Desmond functions are reported, not silently kept
    assert any("unknown variable" in m
               for m in check_expression("q*2.0", known))
    assert any("does not exist" in m
               for m in check_expression("abs(z)", known))
    # `abs` and `tan` really are absent, but `min`/`max` are NOT: both are in
    # getFcnSigs() on the installed 2025-3 and 2020-3 suites, and
    # enhsamp.parseStr accepts `min(array(1.0,2.0,3.0))`.  The deny list used
    # to claim otherwise and told users to rewrite working input.  `let` is
    # the real backend-only name: the engine lowers blocks to it, the front
    # end raises KeyError on it.
    assert "abs" in NOT_IN_DESMOND and "tan" in NOT_IN_DESMOND
    assert "min" not in NOT_IN_DESMOND and "max" not in NOT_IN_DESMOND
    assert "let" in NOT_IN_DESMOND
    assert check_expression("min(array(z,4.0))", known) == []
    assert check_expression("max(array(z,4.0))", known) == []
    assert any("front end" in m or "block" in m
               for m in check_expression("let(z)", known))
    # blocks, series and subscripts pass the checker
    assert check_expression("{ a = z*2.0; a+1.0; }", known) == []
    assert check_expression("series (i=0:3) i*z", known) == []
    assert check_expression("array(z,rho,0.0)[1]", known) == []


def test_evaluator_rejects_out_of_range_atoms():
    job = load_job()
    first = job.spec.lig.indices[0]
    bad = job.structure.n_atoms + 1234          # certainly outside 1..N
    text = job.pot_text().replace(f"atom. {first},", f"atom. {bad},")
    assert str(bad) in text, "the substitution must actually have happened"
    try:
        evaluate_pot(text, job.structure)
    except MExprError as exc:
        assert str(bad) in str(exc)
        return
    raise AssertionError("an out-of-range atom index must be rejected")


# ---------------------------------------------------------------- edits
def test_edited_spec_exports_and_verifies():
    job = load_job()
    spec = job.spec
    saved = spec.copy()
    try:
        spec.z_cc = 22.5
        spec.r_cyl = 5.25
        spec.set_cone_angle_deg(27.0)
        spec.z_min = -6.0
        spec.z_max = 30.0
        spec.k_rad = 40.0
        spec.k_z = 60.0
        spec.h0 = 0.05
        spec.sigma_z = 0.35
        spec.gamma = 12.0
        spec.sync_ktemp()
        for key in ("z_cc", "r_cyl", "cone_slope", "z_min", "z_max", "k_rad",
                    "k_z", "h0", "sigma_z"):
            spec.literals.pop(key, None)
        job.refresh_model()
        info = job.verify_pot_text(job.pot_text())
        assert info["ok"], info
        text = job.pot_text()
        assert "z_cc = 22.5;" in text and "k_rad = 40.0;" in text
        assert f"ktemp = {spec.ktemp!r};" in text
        assert "# Well-tempered bias: gamma=12 at 310 K." in text
    finally:
        job.spec = saved
        job.refresh_model()


def test_two_dimensional_potential_is_valid():
    job = load_job()
    saved = job.spec.copy()
    try:
        sp = job.spec
        sp.dimension = 2
        sp.sigma_rho = 0.4
        text = job.pot_text()
        assert "dimension = 2," in text
        assert "v_old = meta(0,array(0.0,0.0,0.0),array(z,rho));" in text
        assert "v_meta = meta(0,array(hill,sigma_z,sigma_rho),array(z,rho));" in text
        assert "sigma_rho = 0.4;" in text
        info = job.verify_pot_text(text)
        assert info["ok"], info
        spec2, rep2 = parse_pot(text)
        assert rep2.ok and spec2.dimension == 2
        assert abs(spec2.sigma_rho - 0.4) < 1e-12
    finally:
        job.spec = saved
        job.refresh_model()


def test_selection_change_updates_comments_and_verifies():
    job = load_job()
    saved = job.spec.copy()
    try:
        st = job.structure
        # four CA atoms from four protein residues of this structure
        ca = np.where(st.mask("protein") &
                      np.array([str(n) == "CA" for n in st.atomname]))[0]
        idx = ca[:: max(1, len(ca) // 5)][:4]
        job.spec.site.indices = sorted(int(i) + 1 for i in idx)
        job.refresh_model()
        text = job.pot_text()
        want = ", ".join(st.residue_signature(np.asarray(idx)))
        assert f"# SITE: {want}." in text, want
        assert job.verify_pot_text(text)["ok"]
        assert f'site = atomsel("atom. {",".join(str(i) for i in job.spec.site.indices)}");' in text
    finally:
        job.spec = saved
        job.refresh_model()


def test_added_diagnostic_round_trips():
    job = load_job()
    saved = job.spec.copy()
    try:
        sp = job.spec
        before = [x.var for x in sp.diagnostics]
        atom = int(job.spec.core.indices[0])     # any real atom of this system
        d = Diagnostic(var="probe1", label="Probe1", indices=[atom],
                       dist_var="d_probe1")
        sp.diagnostics.append(d)
        sp.prints.insert(len(sp.prints) - 1, (d.print_label, d.dist_var))
        text = job.pot_text()
        assert f'probe1_sel = atomsel("atom. {atom}");' in text
        assert "d_probe1 = norm(min_image(lig_com-probe1));" in text
        assert 'print("Probe1_to_ligCOM_A",d_probe1);' in text
        spec2, rep2 = parse_pot(text)
        assert rep2.ok
        assert [x.var for x in spec2.diagnostics] == before + ["probe1"]
        assert job.verify_pot_text(text)["ok"]
    finally:
        job.spec = saved
        job.refresh_model()


def test_validation_finds_planted_problems():
    job = load_job()
    saved = job.spec.copy()
    try:
        issues = job.validate()
        assert not [i for i in issues if i.level == "error"], \
            [i.message for i in issues if i.level == "error"]
        sp = job.spec
        sp.z_max = 300.0                   # outside the box
        sp.z_min = 20.0                    # ligand now outside the walls
        job.refresh_model()
        msgs = " | ".join(i.message for i in job.validate()
                          if i.level == "error")
        assert "outside the axial walls" in msgs
        assert "leaves the periodic box" in msgs
        sp2 = job.spec
        sp2.z_min, sp2.z_max = -4.0, 34.0
        sp2.lig.indices = [1, 2, 3]        # protein atoms as the "ligand"
        st = job.structure
        first_water = int(np.where(st.mask("water"))[0][0]) + 1
        sp2.site.indices = [first_water]   # a water atom in the frame
        job.refresh_model()
        msgs = " | ".join(i.message for i in job.validate())
        assert "solvent atom" in msgs
    finally:
        job.spec = saved
        job.refresh_model()


def test_autobuild_from_bare_structure():
    """A brand-new system: guess the groups, fit a funnel, verify the file."""
    from funnelforge.core.autobuild import suggest_groups
    st = load_job().structure
    sug = suggest_groups(st)
    # it finds a real, contiguous, non-solvent small molecule
    assert sug.lig.n > 5
    assert sug.lig.indices == list(range(sug.lig.indices[0],
                                         sug.lig.indices[-1] + 1))
    picked = {st.residue_of_atom(i - 1).long_label for i in sug.lig.indices}
    assert len(picked) == 1, picked
    assert not st.mask("water")[np.asarray(sug.lig.idx0())].any()
    assert sug.label and sug.label == st.residue_of_atom(
        sug.lig.indices[0] - 1).resname.strip()
    assert sug.site.n == 16 and sug.core.n == 16
    assert all(str(st.atomname[i - 1]) in ("N", "CA", "C", "O")
               for i in sug.site.indices + sug.core.indices)
    assert any("ligand:" in n for n in sug.notes)

    spec = FunnelSpec()
    spec.lig, spec.site, spec.core = sug.lig, sug.site, sug.core
    spec.ligand_label = sug.label
    spec.ktemp = spec.ktemp_from_gamma()
    model = FunnelModel(st, spec)
    assert model.frame.axis_len > 8.0

    # the same geometry heuristic the GUI button uses
    z0, rho0 = model.cv_of_ligand()
    from scipy.spatial import cKDTree
    heavy = np.where(st.mask("heavy") & ~st.mask("water"))[0]
    tree = cKDTree(st.xyz[heavy])
    zs = np.arange(z0, z0 + 90.0, 0.5)
    d, _ = tree.query(model.frame.origin + zs[:, None] * model.frame.axis, k=1)
    win = 20
    z_cc = next((float(zs[k]) for k in range(len(zs) - win)
                 if np.all(d[k:k + win] > spec.r_cyl + 1.5)),
                float(zs[int(np.argmax(d))]))
    spec.z_min = float(np.floor(z0 - 3.0))
    spec.z_cc = z_cc
    spec.cone_slope = max(np.tan(np.radians(30.0)),
                          (rho0 + 4.0 - spec.r_cyl) / max(z_cc - spec.z_min, 1e-6))
    z_max = z_cc + 8.0
    for _ in range(60):
        spec.z_max = z_max
        model.invalidate()
        clear, _p = model.funnel_clearance()
        if clear >= 9.0 or z_max <= z_cc + 1.0:
            break
        z_max -= 1.0
    model.invalidate()
    z0, rho0 = model.cv_of_ligand()
    assert spec.z_min < z0 < spec.z_max
    assert float(spec.wall_potential(z0, rho0)) == 0.0

    job = Job()
    job.structure = st
    job.spec = spec
    job.refresh_model()
    text = job.pot_text()
    first = sug.lig.indices[0]
    assert f'lig = atomsel("atom. {first},' in text
    assert job.verify_pot_text(text)["ok"]
    spec2, rep2 = parse_pot(text)
    assert rep2.ok and not rep2.unknown_statements


# ---------------------------------------------------------------- shape terms
def _with_terms(job, terms):
    """Install terms on a job and give the caller the generated text."""
    saved = job.spec.copy()
    job.spec.terms = list(terms)
    job.refresh_model()
    return saved


def test_lathe_term_emits_valid_desmond_and_verifies():
    from funnelforge.core.terms import make_term
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        t = make_term("lathe", sp)
        t.label = "Bulb"
        t.profile = "5.0+7.0*exp(0.0-((z-8.0)^2)/40.0)"
        t.z_lo, t.z_hi = -4.0, 24.0
        t.k, t.cap, t.k_cap = 30.0, "solvent", 60.0
        sp.terms = [t]
        job.refresh_model()
        text = job.pot_text()
        assert "w1_r0 = 5.0+7.0*exp(0.0-((z-8.0)^2)/40.0);" in text
        assert "w1_r = if w1_r0 then w1_r0 else 0.0;" in text
        assert "w1_e = if rho-w1_r then rho-w1_r else 0.0;" in text
        assert "w1_wall = 0.5*30.0*w1_e^2;" in text
        assert "w1_hi = if z-24.0 then z-24.0 else 0.0;" in text
        assert "v_w1 = w1_wall+w1_cap;" in text
        assert "v_total = v_rad+v_z+v_meta+v_w1;" in text
        info = job.verify_pot_text(text)
        assert info["ok"], info
        # the profile really is what the lathe turns
        r = job.model.term_profile(t, np.array([8.0, -4.0]))
        assert abs(r[0] - 12.0) < 1e-9
        assert abs(r[1] - (5.0 + 7.0 * np.exp(-(144.0) / 40.0))) < 1e-9
    finally:
        job.spec = saved
        job.refresh_model()


def test_expression_term_is_emitted_verbatim_and_verifies():
    from funnelforge.core.terms import make_term
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        t = make_term("expression", sp)
        t.label = "Egg"
        t.expression = ("0.5*20.0*(if sqrt(((z-14.0)^2)/4.0+(rho^2))-7.0 then "
                        "sqrt(((z-14.0)^2)/4.0+(rho^2))-7.0 else 0.0)^2")
        sp.terms = [t]
        job.refresh_model()
        text = job.pot_text()
        assert f"v_w1 = {t.expression};" in text
        info = job.verify_pot_text(text)
        assert info["ok"], info
        # and the number it gives is the ellipsoid formula, checked by hand
        z0, rho0 = job.model.cv_of_ligand()
        d = np.sqrt((z0 - 14.0) ** 2 / 4.0 + rho0 ** 2)
        expect = 0.5 * 20.0 * max(0.0, d - 7.0) ** 2
        ctx = job.model.field_ctx(points=job.model.frame.lig_com[None, :])
        got = float(np.asarray(t.energy(ctx)).reshape(-1)[0])
        assert abs(got - expect) < 1e-9
    finally:
        job.spec = saved
        job.refresh_model()


def test_region_switch_is_continuous_and_localised():
    from funnelforge.core.terms import make_term
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        t = make_term("lathe", sp)
        t.label = "Tight channel"
        t.profile = "2.2"
        t.k, t.cap = 120.0, "none"
        t.region.enabled = True
        t.region.z_lo, t.region.z_hi, t.region.taper = 24.0, 34.0, 2.5
        sp.terms = [t]
        job.refresh_model()
        text = job.pot_text()
        assert "w1_s = w1_sac^2*(3.0-2.0*w1_sac)*w1_sbc^2*(3.0-2.0*w1_sbc);" \
            in text
        assert "v_w1 = w1_s*w1_wall;" in text
        assert "z--" not in text            # never emit a double minus
        info = job.verify_pot_text(text)
        assert info["ok"], info
        # the switch is zero outside, one in the middle, and continuous
        sw = t.region.switch(np.array([20.0, 24.0, 29.0, 34.0, 40.0]))
        assert sw[0] == 0.0 and sw[3] == 0.0 and sw[4] == 0.0
        assert abs(sw[2] - 1.0) < 1e-12
        fine = t.region.switch(np.linspace(20.0, 38.0, 4000))
        assert np.abs(np.diff(fine)).max() < 0.01      # no jumps
        # the term only bites inside its region
        ctx = job.model.field_ctx(z=np.array([10.0, 29.0]),
                                  rho=np.array([6.0, 6.0]))
        e = np.asarray(t.energy(ctx))
        assert e[0] == 0.0 and e[1] > 1.0
    finally:
        job.spec = saved
        job.refresh_model()


def test_sphere_and_slab_terms_and_group_targets():
    from funnelforge.core.terms import make_term
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        s1 = make_term("sphere", sp)
        s1.label = "Pocket sphere"
        s1.center_mode, s1.center_indices = "group", [1471]
        s1.radius, s1.k = 20.0, 5.0
        s2 = make_term("slab", sp, existing=["w1"])
        s2.label = "Axial window"
        s2.z_lo, s2.z_hi, s2.k = -6.0, 30.0, 40.0
        s3 = make_term("sphere", sp, existing=["w1", "w2"])
        s3.label = "Hold a loop"
        s3.target_kind, s3.target_indices = "selection", [815, 816, 817, 818]
        s3.center_mode, s3.center_z, s3.radius, s3.k = "axis", 4.0, 30.0, 2.0
        s3.region.enabled = True
        s3.region.z_lo, s3.region.z_hi, s3.region.taper = -10.0, 40.0, 2.0
        sp.terms = [s1, s2, s3]
        job.refresh_model()
        text = job.pot_text()
        assert 'w1_csel = atomsel("atom. 1471");' in text
        assert "w1_c = center_of_mass(w1_csel);" in text
        assert "w1_dc = min_image(lig_com-w1_c);" in text
        assert "w2_lo = if -6.0-z then -6.0-z else 0.0;" in text
        assert 'w3_grp = atomsel("atom. 815,816,817,818");' in text
        assert "w3_com = center_of_mass(w3_grp);" in text
        assert "w3_z = dot(w3_dv,axis);" in text       # needed by its region
        # a sphere with no region does not drag in unused CV statements
        s4 = make_term("sphere", sp, existing=["w1", "w2", "w3"])
        s4.target_kind, s4.target_indices = "selection", [815]
        s4.center_mode, s4.center_z = "axis", 0.0
        sp.terms = sp.terms + [s4]
        job.refresh_model()
        text2 = job.pot_text()
        assert "w4_com = center_of_mass(w4_grp);" in text2
        assert "w4_z = dot" not in text2
        assert "w4_c = origin;" in text2               # not origin+0.0*axis
        assert job.verify_pot_text(text2)["ok"]
        sp.terms = sp.terms[:3]
        job.refresh_model()
        text = job.pot_text()
        assert "v_total = v_rad+v_z+v_meta+v_w1+v_w2+v_w3;" in text
        info = job.verify_pot_text(text)
        assert info["ok"], info
        # sphere distance is checked against a hand computation
        st = job.structure
        centre = st.center_of_mass(np.array([1470]))
        d = np.linalg.norm(st.min_image(job.model.frame.lig_com - centre))
        expect = 0.5 * 5.0 * max(0.0, d - 20.0) ** 2
        ctx = job.model.field_ctx(points=job.model.frame.lig_com[None, :])
        assert abs(float(np.asarray(s1.energy(ctx)).reshape(-1)[0])
                   - expect) < 1e-9
    finally:
        job.spec = saved
        job.refresh_model()


def test_terms_round_trip_and_disabled_terms_survive():
    from funnelforge.core.terms import make_term
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        a = make_term("lathe", sp)
        a.label, a.profile = "Keep", "7.5"
        b = make_term("expression", sp, existing=["w1"])
        b.label, b.expression, b.enabled = "Parked", "0.5*3.0*(rho)^2", False
        sp.terms = [a, b]
        job.refresh_model()
        text = job.pot_text()
        assert "v_total = v_rad+v_z+v_meta+v_w1;" in text     # b is parked
        assert "disabled, not part of the potential" in text
        spec2, rep2 = parse_pot(text)
        assert rep2.ok, rep2.errors
        assert not rep2.unknown_statements, rep2.unknown_statements
        assert [t.label for t in spec2.terms] == ["Keep", "Parked"]
        assert [t.enabled for t in spec2.terms] == [True, False]
        assert spec2.terms[0].profile == "7.5"
        assert spec2.terms[1].expression == "0.5*3.0*(rho)^2"
        assert emit_pot(spec2, job.structure) == text          # stable
    finally:
        job.spec = saved
        job.refresh_model()


def test_custom_total_contribution_is_preserved_and_evaluated():
    job = load_job()
    text = job.pot_text().replace("v_total = v_rad+v_z+v_meta;",
                                  "v_mystery = 1.0;\n"
                                  "v_total = v_rad+v_z+v_meta+v_mystery;")
    spec2, rep2 = parse_pot(text)
    assert rep2.ok, rep2.errors
    assert spec2.source_adapter
    assert emit_pot(spec2, job.structure) == text
    from funnelforge.core.funnel import FunnelModel
    model = FunnelModel(job.structure, spec2)
    assert abs(model.total_at_ligand() - (job.model.total_at_ligand() + 1.0)) < 1e-9
    spec2.k_rad += 1.0
    edited = emit_pot(spec2, job.structure)
    assert "v_mystery = 1.0;" in edited
    assert "v_total = v_rad+v_z+v_meta+v_mystery;" in edited


def test_funnel_can_be_switched_off():
    from funnelforge.core.terms import make_term
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        t = make_term("lathe", sp)
        t.label, t.profile, t.cap = "Only wall", "9.0", "both"
        sp.terms = [t]
        sp.funnel_enabled = False
        job.refresh_model()
        text = job.pot_text()
        assert "z_cc" not in text and "v_rad" not in text
        assert "v_total = v_meta+v_w1;" in text
        assert "funnel_radius_A" not in text        # print was dropped
        assert "built-in funnel is switched off" in text
        info = job.verify_pot_text(text)
        assert info["ok"], info
        spec2, rep2 = parse_pot(text)
        assert rep2.ok and spec2.funnel_enabled is False
        assert len(spec2.terms) == 1
    finally:
        job.spec = saved
        job.refresh_model()


def test_term_validation_catches_mistakes():
    from funnelforge.core.terms import make_term
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        bad = make_term("expression", sp)
        bad.expression = "abs(rho)*max(z,0.0)"
        sp.terms = [bad]
        job.refresh_model()
        msgs = " | ".join(i.message for i in job.validate())
        assert "abs()" in msgs and "max()" in msgs
        bad.expression = "0.5*10.0*(rho-4.0)^2.5"
        msgs = " | ".join(i.message for i in job.validate())
        assert "pow(" in msgs
        bad.expression = "0.5*10.0*(nonsense-4.0)^2"
        msgs = " | ".join(i.message for i in job.validate())
        assert "unknown variable" in msgs
        # a hard gate is flagged
        bad.expression = "0.0"
        bad.region.enabled = True
        bad.region.taper = 0.0
        msgs = " | ".join(i.message for i in job.validate())
        assert "hard gate" in msgs
        # duplicated variables are flagged
        other = make_term("slab", sp)
        other.var = bad.var
        sp.terms = [bad, other]
        msgs = " | ".join(i.message for i in job.validate())
        assert "both use the variable" in msgs
    finally:
        job.spec = saved
        job.refresh_model()


def test_total_field_and_bounds():
    from funnelforge.core.terms import make_term
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        t = make_term("sphere", sp)
        t.center_z, t.radius, t.k = 10.0, 6.0, 8.0
        sp.terms = [t]
        job.refresh_model()
        m = job.model
        lo, hi, r_out = m.field_bounds(pad=2.0)
        assert lo <= sp.z_min and hi >= sp.z_max and r_out >= sp.r_base()
        xs, ys, zs, V = m.sample_field_grid(n_axial=32, n_radial=24)
        assert V.shape == (24, 24, 32)
        assert float(V.min()) == 0.0 and float(V.max()) > 0.0
        # the combined field equals the file, sampled independently
        rng = np.random.default_rng(3)
        pts = m.frame.cyl_to_world(rng.uniform(lo, hi, 500),
                                   rng.uniform(0, r_out, 500),
                                   rng.uniform(0, 2 * np.pi, 500))
        from funnelforge.core.mexpr import evaluate_pot
        res = evaluate_pot(job.pot_text(), job.structure, probe=pts)
        assert np.abs(np.asarray(m.total_field(points=pts))
                      - res.array("v_total")).max() < 1e-8
    finally:
        job.spec = saved
        job.refresh_model()


def test_a_job_without_a_pot_has_no_funnel():
    """Importing a .cms on its own must not invent (or remember) a funnel."""
    from funnelforge.core.funnel import FunnelSpec
    fresh = FunnelSpec()
    assert fresh.funnel_enabled is False
    assert (fresh.z_min, fresh.z_max, fresh.z_cc, fresh.r_cyl) == \
        (0.0, 0.0, 0.0, 0.0)
    assert fresh.terms == []

    # a .cms on its own, with no sibling job files to be found
    lonely_dir = tempfile.mkdtemp(prefix="ff_lonely_")
    lonely = os.path.join(lonely_dir, "structure_only.cms")
    os.symlink(load_job().structure.path, lonely)

    job = Job()
    job.import_bundle(lonely)
    assert job.spec.funnel_enabled is False
    assert job.model.funnel_is_degenerate()
    text = job.pot_text()
    for token in ("z_cc", "r_cyl", "v_rad", "v_z", "cone_slope"):
        assert token not in text, token
    assert "v_total = v_meta;" in text
    msgs = " | ".join(i.message for i in job.validate())
    assert "No restraint is defined at all" in msgs

    # and a second import does not carry the first job's funnel over
    job2 = Job()
    job2.import_bundle(JOB)
    assert job2.spec.funnel_enabled is True
    fitted_z_cc = job2.spec.z_cc
    assert fitted_z_cc != 0.0
    job2.import_bundle(lonely)
    assert job2.spec.funnel_enabled is False
    assert job2.spec.z_cc == 0.0 != fitted_z_cc
    assert job2.spec.terms == []
    # importing a .cms that *does* have a sibling .pot still picks it up
    job3 = Job()
    job3.import_bundle(load_job().structure.path)
    assert job3.spec.funnel_enabled is True


def test_funnel_can_be_added_and_is_fitted_to_the_pocket():
    from funnelforge.core.autobuild import suggest_groups
    job = Job()
    job.attach_structure(load_job().structure.path)
    sug = suggest_groups(job.structure)
    job.spec.lig, job.spec.site, job.spec.core = sug.lig, sug.site, sug.core
    job.refresh_model()
    rep = job.model.initialise_funnel(cutoff=9.0)
    sp = job.spec
    assert sp.funnel_enabled and not job.model.funnel_is_degenerate()
    z0, rho0 = job.model.cv_of_ligand()
    assert sp.z_min < z0 < sp.z_max
    assert rho0 < sp.r_allowed(z0)
    assert rep["start_energy"] == 0.0
    assert rep["clearance"] >= 9.0 or rep["z_max"] <= rep["z_cc"] + 1.0
    # z_cc is placed where the axis really has left the protein behind
    assert rep["free_at_zcc"] >= sp.r_cyl + 1.0
    zs, r_free = job.model.free_radius_profile(sp.z_cc, sp.z_max)
    assert float(np.min(r_free)) > sp.r_cyl, float(np.min(r_free))
    # a 30 degree cone unless the ligand needed more
    assert 25.0 <= sp.cone_angle_deg <= 75.0
    assert job.verify_pot_text(job.pot_text())["ok"]
    text = job.pot_text()
    assert "z_cc" in text and "v_rad" in text


def test_manual_placement_is_exact_and_round_trips():
    """Sideways placement uses a body-fixed frame and survives export."""
    from funnelforge.core.terms import make_term
    from funnelforge.core.autobuild import suggest_frame_reference
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        idx = suggest_frame_reference(job.structure, sp, job.model)
        assert idx, "no frame reference could be picked"
        sp.frame_ref.indices = idx
        job.model.invalidate()
        fr = job.model.frame
        # the basis really is orthonormal and body-fixed
        assert abs(float(fr.e1 @ fr.axis)) < 1e-12
        assert abs(float(fr.e2 @ fr.axis)) < 1e-12
        assert abs(float(fr.e1 @ fr.e2)) < 1e-12
        assert abs(np.linalg.norm(fr.e1) - 1.0) < 1e-12

        t = make_term("lathe", sp)
        t.label, t.profile, t.cap, t.k = "Tube", "5.0", "none", 25.0
        t.offset_e1, t.offset_e2 = 2.5, 0.0
        sp.terms = [t]
        job.refresh_model()
        m = job.model
        fr = m.frame

        # a point 4 A off the axis on the far side is 6.5 A from the tube axis
        p = fr.origin + 5.0 * fr.axis - 4.0 * fr.e1
        ctx = m.field_ctx(points=p[None, :])
        got = float(np.asarray(t.energy(ctx)).reshape(-1)[0])
        assert abs(got - 0.5 * 25.0 * 1.5 ** 2) < 1e-9
        # centred again, the same point is inside
        t.offset_e1 = 0.0
        assert float(np.asarray(t.energy(m.field_ctx(points=p[None, :])))
                     .reshape(-1)[0]) == 0.0
        t.offset_e1 = 2.5

        # the emitted file agrees, and defines e1/e2 from the reference group
        text = job.pot_text()
        assert 'fref = atomsel(' in text
        assert "e1 = fref_perp/norm(fref_perp);" in text
        assert "e2 = cross(axis,e1);" in text
        assert "w1_ax = origin+2.5*e1;" in text
        assert "w1_dv = min_image(lig_com-w1_ax);" in text
        info = job.verify_pot_text(text)
        assert info["ok"], info
        spec2, rep2 = parse_pot(text)
        assert rep2.ok and not rep2.unknown_statements
        assert spec2.frame_ref.n == len(idx)
        assert abs(spec2.terms[0].offset_e1 - 2.5) < 1e-12
        assert emit_pot(spec2, job.structure) == text

        # sliding along the axis keeps the size
        length = t.z_hi - t.z_lo
        t.move_axially(t.axial_position() + 3.0)
        assert abs((t.z_hi - t.z_lo) - length) < 1e-12
        assert job.verify_pot_text(job.pot_text())["ok"]

        # a lateral offset with no reference group is refused, not silently run
        sp.frame_ref.indices = []
        msgs = " | ".join(i.message for i in job.validate()
                          if i.level == "error")
        assert "off the axis" in msgs and "frame reference" in msgs
    finally:
        job.spec = saved
        job.refresh_model()


def test_lateral_offset_leaves_z_untouched():
    """e1 and e2 are perpendicular to the axis, so z cannot move."""
    from funnelforge.core.autobuild import suggest_frame_reference
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        sp.frame_ref.indices = suggest_frame_reference(job.structure, sp,
                                                      job.model)
        job.model.invalidate()
        z_before, _rho = job.model.cv_of_ligand()
        sp.origin_lat1, sp.origin_lat2 = 1.75, -2.5
        job.model.invalidate()
        z_after, rho_after = job.model.cv_of_ligand()
        assert abs(z_after - z_before) < 1e-9
        assert rho_after != _rho
        text = job.pot_text()
        base = next(l.split("=", 1)[1].split("+e1")[0].strip().rstrip(";")
                    for l in text.splitlines()
                    if l.startswith("origin = "))
        head = base.split("+1.75*e1")[0]
        assert f"origin = {head}+1.75*e1-2.5*e2;" in text, base
        assert job.verify_pot_text(text)["ok"]
        spec2, _rep = parse_pot(text)
        assert abs(spec2.origin_lat1 - 1.75) < 1e-12
        assert abs(spec2.origin_lat2 + 2.5) < 1e-12
    finally:
        job.spec = saved
        job.refresh_model()


def test_per_term_levels_and_gradation():
    from funnelforge.core.terms import make_term
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        t = make_term("lathe", sp)
        t.k, t.exponent = 40.0, 2
        assert [round(e, 3) for _d, e in t.gradation((1.0, 2.0))] == \
            [20.0, 80.0]
        t.exponent = 4
        assert [round(e, 3) for _d, e in t.gradation((1.0, 2.0))] == \
            [20.0, 320.0]
        t.own_levels, t.level_mode, t.levels = True, "energy", [5.0, 25.0]
        assert abs(t.radial_offset_for(5.0) - (2 * 5.0 / 40.0) ** 0.25) < 1e-12
        sp.terms = [t]
        job.refresh_model()
        text = job.pot_text()
        assert "w1_e^4;" in text
        assert job.verify_pot_text(text)["ok"]
        spec2, _rep = parse_pot(text)
        assert spec2.terms[0].own_levels is True
        assert spec2.terms[0].levels == [5.0, 25.0]
        assert spec2.terms[0].exponent == 4
    finally:
        job.spec = saved
        job.refresh_model()


def test_nothing_can_be_placed_outside_the_solvent_box():
    """Shapes are confined to the imported cell, by clamping and by check."""
    from funnelforge.core.terms import make_term
    job = load_job()
    m, sp = job.model, job.spec
    saved = sp.copy()
    try:
        # clamping is a fixed point: whatever it changes the first time, a
        # second pass has nothing left to do, and nothing sticks out after it
        m.clamp_to_box()
        job.refresh_model()
        m = job.model
        assert m.outside_box() == []
        assert m.clamp_to_box() == []
        # clamping is an edit like any other, so put the job back before the
        # checks below, which assert the untouched file re-emits unchanged
        job.spec = saved.copy()
        job.refresh_model()
        m, sp = job.model, job.spec
        assert emit_pot(sp, job.structure) == open(JOB).read()

        # the limits agree with the cell
        half = np.linalg.norm(job.structure.box, axis=1) / 2.0
        lo, hi = m.axis_limits(margin=0.0)
        fr = m.frame
        for t in (lo, hi):
            p = fr.origin + t * fr.axis
            assert np.all(np.abs(p) <= half + 1e-6), (t, p)
        assert m.best_room() < float(half.min())

        # a tilted, offset shape is pulled back by the surface backstop
        from funnelforge.core.autobuild import suggest_frame_reference
        sp_t = saved.copy()
        job.spec = sp_t
        job.refresh_model()
        sp_t.frame_ref.indices = suggest_frame_reference(job.structure, sp_t,
                                                        job.model)
        tilted = make_term("lathe", sp_t)
        tilted.profile, tilted.cap = "8.0", "none"
        tilted.z_lo, tilted.z_hi = -30.0, 60.0
        tilted.tilt_deg, tilted.azimuth_deg = 55.0, 20.0
        tilted.offset_e1 = 30.0
        sp_t.terms = [tilted]
        job.model.invalidate()
        assert job.model.term_overshoot(tilted, 0.25) > 0
        job.model.clamp_to_box()
        assert job.model.term_overshoot(tilted, 0.25) <= 1e-6
        assert job.model.outside_box() == []
        assert job.verify_pot_text(job.pot_text())["ok"]

        # absurd geometry is pulled all the way back in
        for absurd in (dict(z_max=300.0, z_min=-250.0, r_cyl=120.0),
                       dict(z_max=60.0, z_min=-60.0, r_cyl=40.0,
                            cone_slope=3.0),
                       dict(z_cc=-200.0, z_max=250.0)):
            sp2 = saved.copy()
            job.spec = sp2
            job.refresh_model()
            for k, v in absurd.items():
                setattr(sp2, k, v)
            ball = make_term("sphere", sp2)
            ball.center_z, ball.radius = 200.0, 90.0
            tube = make_term("lathe", sp2, existing=["w1"])
            tube.z_lo, tube.z_hi, tube.profile = -200.0, 250.0, "60.0"
            sp2.terms = [ball, tube]
            job.model.invalidate()
            assert job.model.outside_box(), absurd
            pulled = job.model.clamp_to_box()
            assert pulled
            assert job.model.outside_box() == [], (absurd, pulled)
            # and the potential still says what the model says
            assert job.verify_pot_text(job.pot_text())["ok"]
            # a profile too wide for the cell is capped in the file itself
            assert tube.clamp_radius and tube.radius_cap > 0
            text = job.pot_text()
            cap = f"{tube.radius_cap!r}"
            assert f"w2_r = if {cap}-w2_r1 then w2_r1 else {cap};" in text

        # an imported file that is outside is reported, not silently rewritten
        sp3 = saved.copy()
        job.spec = sp3
        job.refresh_model()
        sp3.z_max = 250.0
        job.model.invalidate()
        errs = [i.message for i in job.validate() if i.level == "error"]
        assert any("sticks out" in e for e in errs), errs
        assert sp3.z_max == 250.0          # untouched by validation
    finally:
        job.spec = saved
        job.refresh_model()


def test_handle_values_are_clamped_to_the_box():
    from funnelforge.core.terms import make_term
    from funnelforge.core.autobuild import suggest_frame_reference
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        sp.frame_ref.indices = suggest_frame_reference(job.structure, sp,
                                                      job.model)
        t = make_term("sphere", sp)
        t.center_z, t.radius = 0.0, 8.0
        sp.terms = [t]
        job.refresh_model()
        m = job.model
        lo, hi = m.axis_limits(radius=t.radius, margin=0.25)
        # dragging far past the wall stops at the wall
        assert abs(job.clamp_handle_value(f"t:{t.var}:axial", 500.0)
                   - hi) < 1e-6
        assert abs(job.clamp_handle_value(f"t:{t.var}:axial", -500.0)
                   - lo) < 1e-6
        # the sideways limit carries the body, not just the centre
        a, b = m.lateral_limits(1, radius=t.radius, margin=0.25)
        got = job.clamp_handle_value(f"t:{t.var}:e1", 900.0)
        assert abs(got - b) < 1e-6, (got, b)
        t.offset_e1 = got
        m.invalidate()
        assert m.term_overshoot(t, 0.25) <= 1e-6
        t.offset_e1 = 0.0
        m.invalidate()
        assert job.clamp_handle_value(f"t:{t.var}:R", 900.0) <= \
            m.term_max_radius(t, 0.25) + 1e-6
        assert abs(job.clamp_handle_value("z_max", 1e4)
                   - m.axis_limits(radius=sp.r_cyl, margin=0.25)[1]) < 1e-6
        # a value already inside is returned untouched
        assert abs(job.clamp_handle_value("z_max", 30.0) - 30.0) < 1e-12
    finally:
        job.spec = saved
        job.refresh_model()


def test_shapes_and_funnel_can_be_tilted():
    """Angles are written as body-fixed constants and survive a round trip."""
    from funnelforge.core.terms import make_term, tilt_coefficients
    from funnelforge.core.autobuild import suggest_frame_reference
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        sp.frame_ref.indices = suggest_frame_reference(job.structure, sp,
                                                      job.model)
        assert sp.frame_ref.n > 0
        axis0 = job.model.frame.axis.copy()

        # a tilted shape
        t = make_term("lathe", sp)
        t.label, t.profile, t.cap = "Tilted tube", "5.0", "none"
        t.tilt_deg, t.azimuth_deg = 25.0, -30.0
        t.offset_e1, t.offset_axial = 2.0, 1.5
        sp.terms = [t]
        # and a tilted funnel axis
        sp.axis_tilt_deg, sp.axis_azimuth_deg = 12.0, 55.0
        job.refresh_model()
        fr = job.model.frame

        # the frame really turned, and stays orthonormal
        got = np.degrees(np.arccos(np.clip(float(fr.axis @ axis0), -1, 1)))
        assert abs(got - 12.0) < 1e-6
        assert abs(float(fr.e1 @ fr.axis)) < 1e-9
        assert abs(np.linalg.norm(fr.e1) - 1.0) < 1e-12
        assert abs(float(np.dot(np.cross(fr.axis, fr.e1), fr.e2)) - 1.0) < 1e-9

        # the shape's own axis is at the requested angle from the funnel axis
        o, u, v1, v2 = job.model.term_frame(t)
        ang = np.degrees(np.arccos(np.clip(float(u @ fr.axis), -1, 1)))
        assert abs(ang - 25.0) < 1e-6
        assert abs(float(u @ v1)) < 1e-9 and abs(float(u @ v2)) < 1e-9

        text = job.pot_text()
        ca, cb, cc = tilt_coefficients(12.0, 55.0)
        assert "axis0 = axis_raw/axis_len;" in text
        assert "p2 = cross(axis0,p1);" in text
        assert f"axis = {ca!r}*axis0+{cb!r}*p1+{cc!r}*p2;" in text
        assert "e1r = p1-dot(p1,axis)*axis;" in text
        wa, wb, wc = tilt_coefficients(25.0, -30.0)
        assert f"w1_u = {wa!r}*axis+{wb!r}*e1{wc!r}*e2;".replace("+-", "-") \
            in text or f"w1_u = {wa!r}*axis+{wb!r}*e1-{abs(wc)!r}*e2;" in text
        assert "w1_z = dot(w1_dv,w1_u);" in text
        assert "w1_ax = origin+1.5*axis+2.0*e1;" in text

        info = job.verify_pot_text(text)
        assert info["ok"], info

        spec2, rep2 = parse_pot(text)
        assert rep2.ok and not rep2.unknown_statements
        assert abs(spec2.axis_tilt_deg - 12.0) < 1e-6
        assert abs(spec2.axis_azimuth_deg - 55.0) < 1e-6
        assert abs(spec2.terms[0].tilt_deg - 25.0) < 1e-9
        assert abs(spec2.terms[0].azimuth_deg - (-30.0)) < 1e-9
        assert emit_pot(spec2, job.structure) == text

        # a tilt with no reference group cannot be written, and is refused
        sp.frame_ref.indices = []
        job.refresh_model()
        msgs = " | ".join(i.message for i in job.validate()
                          if i.level == "error")
        assert "tilted but there is no frame reference" in msgs
        assert "without a frame reference" in msgs
    finally:
        job.spec = saved
        job.refresh_model()


def test_tilted_shape_energy_is_measured_about_its_own_axis():
    from funnelforge.core.terms import make_term
    from funnelforge.core.autobuild import suggest_frame_reference
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        sp.frame_ref.indices = suggest_frame_reference(job.structure, sp,
                                                      job.model)
        t = make_term("lathe", sp)
        t.profile, t.cap, t.k = "4.0", "none", 25.0
        t.z_lo, t.z_hi = -10.0, 20.0
        t.tilt_deg, t.azimuth_deg = 30.0, 0.0
        sp.terms = [t]
        sp.funnel_enabled = False
        job.refresh_model()
        m = job.model
        o, u, v1, v2 = m.term_frame(t)
        # on the shape's own axis the wall is silent, 6 A off it costs
        # 0.5*k*(6-4)^2 whichever way the shape points
        on_axis = o + 8.0 * u
        off_axis = o + 8.0 * u + 6.0 * v1
        ctx_on = m.field_ctx(points=on_axis[None, :])
        ctx_off = m.field_ctx(points=off_axis[None, :])
        assert float(np.asarray(t.energy(ctx_on)).reshape(-1)[0]) == 0.0
        assert abs(float(np.asarray(t.energy(ctx_off)).reshape(-1)[0])
                   - 0.5 * 25.0 * 4.0) < 1e-9
        # the same points measured about the funnel axis would say otherwise
        z_f, rho_f = m.cv(off_axis[None, :])
        assert abs(float(rho_f[0]) - 6.0) > 0.5
        # and the file agrees with the model at both points
        from funnelforge.core.mexpr import evaluate_pot
        res = evaluate_pot(job.pot_text(), job.structure,
                           probe=np.vstack([on_axis, off_axis]))
        assert np.allclose(res.array("v_total"),
                           m.total_field(points=np.vstack([on_axis,
                                                           off_axis])),
                           atol=1e-9)
    finally:
        job.spec = saved
        job.refresh_model()


def test_region_switch_reads_the_funnel_z_even_when_tilted():
    """Regression: the model switched on the shape's own z, the file on the
    funnel z. Identical while nothing was tilted, wrong as soon as it was."""
    from funnelforge.core.terms import make_term
    from funnelforge.core.autobuild import suggest_frame_reference
    from funnelforge.core.mexpr import evaluate_pot
    job = load_job()
    sp = job.spec
    saved = sp.copy()
    try:
        sp.frame_ref.indices = suggest_frame_reference(job.structure, sp,
                                                      job.model)
        t = make_term("lathe", sp, job.model)
        t.profile, t.mode, t.cap = "7.5", "exclude", "both"
        t.k, t.exponent, t.offset, t.k_cap = 40.0, 4, 0.5, 70.0
        t.z_lo, t.z_hi = -2.0, 28.0
        t.tilt_deg, t.azimuth_deg = 20.0, -30.0
        t.offset_e1, t.offset_e2 = 2.0, 1.0
        t.region.enabled = True
        t.region.z_lo, t.region.z_hi, t.region.taper = -6.0, 30.0, 2.0
        sp.terms = [t]
        sp.axis_tilt_deg, sp.axis_azimuth_deg = 8.0, 45.0
        sp.origin_lat1, sp.origin_lat2 = 1.5, -1.0
        job.refresh_model()
        m = job.model
        fr = m.frame

        # a probe where the funnel z and the shape's own z differ a lot
        rng = np.random.default_rng(12345)
        pts = fr.cyl_to_world(rng.uniform(sp.z_min - 8, sp.z_max + 8, 2000),
                              rng.uniform(0, sp.r_base() + 8, 2000),
                              rng.uniform(0, 2 * np.pi, 2000))
        ctx = m.field_ctx(points=pts)
        tz, _rho = ctx.term_cv(t)
        assert float(np.abs(tz - ctx.z).max()) > 1.0, "need a real difference"

        # the switch follows the funnel z, in the model and in the file alike
        sw = t.region.switch(ctx.z)
        res = evaluate_pot(job.pot_text(), job.structure, probe=pts)
        assert np.allclose(res.array(f"{t.var}_s"), sw, atol=1e-9)
        assert np.allclose(np.asarray(m.total_field(points=pts)),
                           res.array("v_total"), atol=1e-6)
        info = job.verify_pot_text(job.pot_text())
        assert info["ok"], info
    finally:
        job.spec = saved
        job.refresh_model()


def test_exported_set_matches_the_desmond_rules():
    """A heavily edited job must come out as five files Desmond will accept."""
    import os
    import re as _re
    import stat as _stat
    import tempfile
    from funnelforge.core.terms import make_term
    from funnelforge.core.funnel import make_diagnostic
    from funnelforge.core.autobuild import suggest_frame_reference
    from funnelforge.core.asl import parse_residue_shorthand
    from funnelforge.core.mexpr import parse as mparse
    from funnelforge.core.blocktext import BlockText

    job = load_job()
    saved = job.spec.copy()
    saved_cfg = job.cfg.text
    saved_msj = job.msj.text
    saved_host, saved_cpu = job.sh.host, job.sh.cpu
    try:
        sp = job.spec
        st = job.structure
        sp.frame_ref.indices = suggest_frame_reference(st, sp, job.model)
        sp.axis_tilt_deg, sp.axis_azimuth_deg, sp.origin_lat1 = 6.0, 30.0, 0.8
        sp.z_cc, sp.r_cyl, sp.k_rad = 24.0, 4.5, 30.0
        a = make_term("lathe", sp, job.model)
        a.label, a.profile, a.cap, a.k = "Tilted tube", "4.5", "none", 60.0
        a.tilt_deg, a.azimuth_deg, a.offset_e1 = 18.0, -25.0, 1.2
        a.region.enabled = True
        a.region.z_lo, a.region.z_hi, a.region.taper = 20.0, 34.0, 2.5
        b = make_term("sphere", sp, job.model, ["w1"])
        b.label, b.radius, b.k = "Cage", 20.0, 3.0
        c = make_term("expression", sp, job.model, ["w1", "w2"])
        c.label = "Bowl"
        c.expression = "0.5*12.0*(if rho-9.0 then rho-9.0 else 0.0)^2"
        sp.terms = [a, b, c]
        # two residues of this structure, one taken whole and one narrowed
        # to an atom it really has
        prot = np.where(st.mask("protein"))[0]
        a1 = int(prot[len(prot) // 3])
        a2 = int(prot[2 * len(prot) // 3])
        r1 = st.residue_of_atom(a1)
        r2 = st.residue_of_atom(a2)
        shorthand = f"{r1.resnum}, {r2.resnum}:{str(st.atomname[a2]).strip()}"
        for g in parse_residue_shorthand(shorthand, st):
            d = make_diagnostic(sp, g["label"], g["indices"])
            sp.diagnostics.append(d)
            sp.prints.insert(len(sp.prints) - 1, (d.print_label, d.dist_var))
        sp.h0, sp.sigma_z, sp.meta_interval = 0.05, 0.4, 1.5
        sp.temperature, sp.gamma = 300.0, 12.0
        sp.sync_ktemp()
        job.cfg.set_number("time", 300000.0)
        job.cfg.set_temperature(300.0)
        job.sh.host, job.sh.cpu = "gpu-01", "4"
        job.refresh_model()
        job.model.clamp_to_box()

        out = tempfile.mkdtemp(prefix="ff_desmond_")
        name = "z5_funnel_v2"
        res = job.export(out, name, cms_mode="symlink")
        assert res.verified, res.verification
        assert not [i for i in res.issues if i.level == "error"], \
            [i.message for i in res.issues if i.level == "error"]
        paths = {e: os.path.join(out, name + e)
                 for e in (".pot", ".msj", ".cfg", ".sh", ".cms")}
        for e, p in paths.items():
            assert os.path.exists(p), e
        assert os.stat(paths[".sh"]).st_mode & _stat.S_IXUSR

        pot = open(paths[".pot"]).read()
        msj = open(paths[".msj"]).read()
        cfg = open(paths[".cfg"]).read()
        sh = open(paths[".sh"]).read()

        # -- the potential, against the language rules
        mparse(pot)                                   # must parse
        res_eval = evaluate_pot(pot, st)              # and evaluate
        assert pot.rstrip().endswith("v_total;")
        assert "declare_meta(" in pot and "declare_output(" in pot
        body = "\n".join(l for l in pot.splitlines()
                          if not l.strip().startswith("#"))
        body_nodecl = _re.sub(r"declare_\w+\s*\((?:[^)]*)\)\s*;", "", body,
                              flags=_re.S)
        assigned = _re.findall(r"^\s*([A-Za-z_]\w*)\s*=", body_nodecl, _re.M)
        assert len(assigned) == len(set(assigned)), \
            [n for n in assigned if assigned.count(n) > 1]
        for label, var in _re.findall(r'print\("([^"]+)",(\w+)\)', body):
            assert var in assigned, (label, var)
        total = _re.search(r"^v_total = ([^;]+);", body, _re.M).group(1)
        for part in total.split("+"):
            assert part.strip() in assigned, part
        assert "$JOBNAME.kerseq" in pot and "$JOBNAME.cvseq" in pot
        assert not _re.search(r"\b(abs|min|max|tan)\s*\(", body_nodecl)
        for m in _re.finditer(r"\^([^\s;)*+\-]+)", body_nodecl):
            assert _re.fullmatch(r"\d+", m.group(1)), m.group(1)

        # -- the stage chain
        mb = BlockText(msj)
        stages = [x for x in mb.root.children if x.key == "simulate"]
        prod = stages[-1]

        def val(node, key):
            n = node.child(key)
            return mb.raw(n).strip('"') if n else None
        assert mb.root.children[0].key == "task" and len(stages) == 7
        assert val(prod, "meta") == "FILE"
        assert val(prod, "meta_file") == name + ".pot"
        assert val(prod, "cfg_file") == name + ".cfg"
        assert val(prod, "jobname") == "$MAINJOBNAME"
        assert (val(prod, "checkpt.write_last_step") or "").lower() in \
            ("yes", "true")
        assert all(val(x, "time") for x in stages[:-1])

        # -- the backend configuration
        cb = BlockText(cfg)
        assert cb.value("meta_file") == "?"
        assert abs(float(cb.value("time")) - 300000.0) < 1e-6
        temp = float(_re.search(r"([\d.]+)",
                                cb.value("temperature")).group(1))
        assert abs(temp - 300.0) < 1e-9 and abs(temp - sp.temperature) < 1e-9
        assert cb.value("randomize_velocity.interval") == "inf"
        assert str(cb.value("checkpt.write_last_step")).lower() == "true"
        assert float(np.linalg.norm(st.box, axis=1).min()) >= \
            2 * float(cb.value("cutoff_radius"))

        # -- the launcher
        assert f'JOBNAME="{name}"' in sh
        for flag in ('-m "${JOBNAME}.msj"', '-c "${JOBNAME}.cfg"',
                     '"${JOBNAME}.cms"', '-o "${JOBNAME}-out.cms"'):
            assert flag in sh, flag
        assert "-HOST gpu-01" in sh and "-cpu 4" in sh
        assert "SCHRODINGER" in sh

        # -- and the whole thing survives a round trip
        j2 = Job()
        rep = j2.import_bundle(paths[".pot"])
        assert rep.pot_report.ok and not rep.pot_report.errors
        assert not rep.pot_report.unknown_statements
        assert len(j2.spec.terms) == 3 and len(j2.spec.diagnostics) == 4
        assert abs(j2.spec.axis_tilt_deg - 6.0) < 1e-12
        assert abs(j2.spec.terms[0].tilt_deg - 18.0) < 1e-12
        assert j2.pot_text() == pot
    finally:
        job.spec = saved
        job.cfg.doc = BlockText(saved_cfg)
        job.msj = type(job.msj)(saved_msj, job.msj.path)
        job.sh.host, job.sh.cpu = saved_host, saved_cpu
        job.refresh_model()


# ---------------------------------------------------------------- job files
def test_blocktext_preserves_and_patches():
    job = load_job()
    for path in (job.paths.msj, job.paths.cfg):
        text = open(path).read()
        assert BlockText(text).text == text
    cfg = CfgFile.load(job.paths.cfg)
    assert cfg.number("time") == 500000.0
    assert cfg.temperature() == 310.0
    assert cfg.value("meta_file") == "?"
    assert cfg.timestep() == [0.002, 0.002, 0.006]
    cfg.set_number("time", 250000.0)
    cfg.set_temperature(300.0)
    assert cfg.number("time") == 250000.0 and cfg.temperature() == 300.0
    assert "annealing = false" in cfg.text          # untouched keys survive
    msj = MsjFile.load(job.paths.msj)
    assert len(msj.stages) == 8
    prod = msj.production
    assert prod is not None and prod.meta_mode == "FILE"
    assert prod.meta_file.endswith(".pot")
    msj.set_meta_file("other.pot")
    assert msj.production.meta_file == "other.pot"
    assert msj.text.count("simulate {") == 7
    sh = ShFile.load(job.paths.sh)
    assert sh.jobname == os.path.splitext(os.path.basename(JOB))[0]
    assert sh.lic == "DESMOND_GPGPU:16" and sh.mode == "umbrella"
    sh.jobname = "renamed"
    out = sh.render()
    assert 'JOBNAME="renamed"' in out and "${JOBNAME}.msj" in out


def test_bundle_discovery():
    b = discover_bundle(JOB)
    for attr in ("cms", "pot", "msj", "cfg", "sh"):
        assert getattr(b, attr), attr


def test_export_writes_consistent_set():
    job = load_job()
    out = tempfile.mkdtemp(prefix="funnelforge_test_")
    res = job.export(out, "renamed_job", cms_mode="symlink")
    assert res.verified, res.verification
    names = sorted(os.path.basename(p) for p in res.written)
    assert names == ["renamed_job.cfg", "renamed_job.cms", "renamed_job.msj",
                     "renamed_job.pot", "renamed_job.sh"]
    msj = open(os.path.join(out, "renamed_job.msj")).read()
    assert 'meta_file = "renamed_job.pot"' in msj
    assert 'cfg_file  = "renamed_job.cfg"' in msj or \
           'cfg_file = "renamed_job.cfg"' in msj
    sh = open(os.path.join(out, "renamed_job.sh")).read()
    assert 'JOBNAME="renamed_job"' in sh
    assert os.access(os.path.join(out, "renamed_job.sh"), os.X_OK)
    # the exported potential still parses and evaluates
    text = open(os.path.join(out, "renamed_job.pot")).read()
    spec2, rep2 = parse_pot(text)
    assert rep2.ok and not rep2.unknown_statements
    assert evaluate_pot(text, job.structure).scalar("v_total") == 0.0
    # nothing was renamed in the .cfg that Multisim fills in
    cfg = open(os.path.join(out, "renamed_job.cfg")).read()
    assert "meta_file = ?" in cfg
    job.sync_files(os.path.splitext(os.path.basename(JOB))[0])


def test_session_round_trip():
    job = load_job()
    saved = job.spec.copy()
    try:
        job.spec.z_cc = 19.0
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        job.save_session(path)
        job2 = Job()
        job2.load_session(path)
        assert abs(job2.spec.z_cc - 19.0) < 1e-12
        assert job2.spec.lig.indices == job.spec.lig.indices
        assert [d.label for d in job2.spec.diagnostics] == \
               [d.label for d in job.spec.diagnostics]
    finally:
        job.spec = saved
        job.refresh_model()


def test_a_bare_structure_still_exports_a_runnable_set():
    """A lone .cms has no .msj/.cfg; export must supply the standard ones."""
    from funnelforge.core.autobuild import suggest_groups
    job = Job()
    job.attach_structure(load_job().structure.path)
    sug = suggest_groups(job.structure)
    job.spec.lig, job.spec.site, job.spec.core = sug.lig, sug.site, sug.core
    job.spec.ligand_label = sug.label
    job.refresh_model()
    job.model.initialise_funnel()
    assert job.msj is None and job.cfg is None

    out = tempfile.mkdtemp(prefix="ff_bare_")
    res = job.export(out, "bare", cms_mode="symlink")
    assert res.verified
    names = sorted(os.path.basename(w) for w in res.written)
    assert names == ["bare.cfg", "bare.cms", "bare.msj", "bare.pot", "bare.sh"], names
    assert any(i.level == "info" and "Created a .msj" in i.message
               for i in res.issues)

    msj = BlockText(open(os.path.join(out, "bare.msj")).read())
    stages = [c for c in msj.root.children if c.key == "simulate"]
    assert len(stages) == 7, len(stages)
    prod = stages[-1]
    assert msj.raw(prod.child("meta")).strip() == "FILE"
    assert msj.raw(prod.child("meta_file")).strip('" ') == "bare.pot"
    assert msj.raw(prod.child("cfg_file")).strip('" ') == "bare.cfg"

    cfg = BlockText(open(os.path.join(out, "bare.cfg")).read())
    assert cfg.value("meta_file").strip() == "?"
    assert abs(cfg.float_value("time") - job.production_ps) < 1e-6
    assert f"{job.spec.temperature:g}" in cfg.value("temperature")

    # the generated set is a real job: it re-imports and validates clean
    j2 = Job()
    j2.import_bundle(os.path.join(out, "bare.pot"))
    assert len(j2.msj.stages) == 8
    errs = [i.message for i in j2.validate() if i.level == "error"]
    assert not errs, errs


def test_random_editing_never_produces_a_broken_file():
    """Fuzz the geometry the way a user drags it, and keep the file valid.

    Every round: move shapes far outside the cell, tilt them, resize their
    windows, add and remove them, then clamp the way the interface does.
    Afterwards nothing may sit outside the box, the validator must report no
    error, and the export must both verify and round-trip byte for byte.
    """
    import random
    from funnelforge.core.terms import make_term
    from funnelforge.core.funnel import make_diagnostic
    from funnelforge.core.autobuild import suggest_frame_reference
    from funnelforge.core.asl import parse_residue_shorthand

    rng = random.Random(20260828)
    job = Job()
    job.import_bundle(JOB)
    st = job.structure
    sp = job.spec
    sp.frame_ref.indices = suggest_frame_reference(st, sp, job.model)

    for it in range(40):
        if rng.random() < 0.55 and len(sp.terms) < 6:
            t = make_term(rng.choice(["lathe", "sphere", "slab", "expression"]),
                          sp, job.model)
            t.k = rng.uniform(0.5, 400.0)
            t.exponent = rng.choice([2, 4, 6])
            t.mode = rng.choice(["confine", "exclude"])
            t.cap = rng.choice(["solvent", "pocket", "both", "none"])
            sp.terms.append(t)
        elif sp.terms:
            t = rng.choice(sp.terms)
            t.offset_axial += rng.uniform(-40, 40)
            t.offset_e1 += rng.uniform(-40, 40)
            t.offset_e2 += rng.uniform(-40, 40)
            t.z_lo += rng.uniform(-20, 20)
            t.z_hi += rng.uniform(-20, 20)
            t.tilt_deg = rng.uniform(0, 80)
            t.azimuth_deg = rng.uniform(0, 360)
            if rng.random() < 0.2 and len(sp.terms) > 1:
                sp.terms.remove(t)
        if rng.random() < 0.3:
            sp.axis_tilt_deg = rng.uniform(-25, 25)
            sp.axis_azimuth_deg = rng.uniform(0, 360)
            sp.origin_lat1 = rng.uniform(-8, 8)
            sp.origin_lat2 = rng.uniform(-8, 8)
            sp.z_cc += rng.uniform(-3, 3)
            sp.r_cyl = max(1.0, sp.r_cyl + rng.uniform(-2, 2))
        if rng.random() < 0.2:
            g = parse_residue_shorthand(
                str(rng.choice([114, 197, 261, 338, 442])), st)
            if g:
                sp.diagnostics.append(
                    make_diagnostic(sp, g[0]["label"], g[0]["indices"]))

        job.refresh_model()
        job.model.clamp_tilts()
        job.model.clamp_to_box()
        job.refresh_model()

        assert not job.model.outside_box(), (it, job.model.outside_box())
        # the two box tests read the same geometry, so they cannot disagree
        clear, _p = job.model.funnel_clearance()
        assert clear > -1e-6, (it, clear)
        for t in sp.terms:
            if t.kind != "sphere":
                assert t.z_hi > t.z_lo, (it, t.var, t.z_lo, t.z_hi)
        errs = [i.message for i in job.validate() if i.level == "error"]
        assert not errs, (it, errs)

        if it % 20 == 19:
            d = tempfile.mkdtemp(prefix="ff_fuzz_")
            res = job.export(d, "fuzz", cms_mode="symlink")
            assert res.verified, [i.message for i in res.issues
                                  if i.level == "error"]
            j2 = Job()
            j2.import_bundle(os.path.join(d, "fuzz.pot"))
            d2 = tempfile.mkdtemp(prefix="ff_fuzz2_")
            j2.export(d2, "fuzz", cms_mode="symlink")
            assert open(os.path.join(d, "fuzz.pot"), "rb").read() == \
                   open(os.path.join(d2, "fuzz.pot"), "rb").read()


def test_verification_tolerance_is_relative_not_absolute():
    """A steep wall reaches 1e9 kcal/mol; one ULP there is already 1e-7."""
    from funnelforge.core.terms import make_term
    job = Job()
    job.import_bundle(JOB)
    sp = job.spec
    t = make_term("sphere", sp, job.model)
    t.k, t.exponent, t.mode, t.radius = 240.0, 6, "exclude", 12.0
    sp.terms.append(t)
    job.refresh_model()
    info = job.verify_pot_text(job.pot_text())
    assert info["ok"], info
    assert info["field_error_rel"] < 1e-12, info["field_error_rel"]
    # and it still catches a genuine disagreement: nudge the emitted force
    # constant itself (not the @ff-term comment, which Desmond never reads)
    text = job.pot_text()
    broken = text.replace("0.5*240.0*", "0.5*240.001*", 1)
    assert broken != text
    assert not job.verify_pot_text(broken)["ok"]


# ---------------------------------------------------------------- runner
def _main() -> int:
    if not HAVE_JOB:
        if _BUILD_ERROR is not None:
            print(f"could not build the designer fixture: {_BUILD_ERROR}")
        else:
            print("no structure in tests/corpus to build the designer "
                  "fixture from; set FUNNELFORGE_TEST_JOB to a .pot")
        return 2
    tests = sorted(((k, v) for k, v in globals().items()
                    if k.startswith("test_") and callable(v)),
                   key=lambda kv: kv[1].__code__.co_firstlineno)
    fails = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            fails += 1
            import traceback
            print(f"  FAIL  {name}: {exc}")
            traceback.print_exc(limit=3)
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())
