"""Custom funnel edits must change the source and preview identically."""
from pathlib import Path

import numpy as np
import pytest

from funnelforge.core.cms import Structure
from funnelforge.core.funnel import Diagnostic, FunnelModel
from funnelforge.core.mexpr import evaluate_pot
from funnelforge.core.potfile import emit_pot, parse_asl_atoms, parse_pot
from funnelforge.core.source_adapter import source_axial, source_frame, source_radius, source_cvs, source_field
from funnelforge.core.terms import Term, FieldCtx


CORPUS = Path(__file__).parent / "corpus" / "ache" / "AChE-G2N_WT_Funnel_MetaD_run"
POT = CORPUS / "AChE-G2N_WT_funnel_MetaD_run.pot"


@pytest.fixture(scope="module")
def structure():
    return Structure.load(str(POT.with_suffix(".cms")))


def imported():
    source = POT.read_text()
    spec, report = parse_pot(source)
    assert report.ok, report.errors
    assert spec.source_adapter
    return source, spec


def test_custom_source_untouched_and_single_scalar_are_lossless():
    source, spec = imported()
    assert emit_pot(spec) == source
    spec.r_cyl = 4.25
    assert emit_pot(spec) == source.replace("r_cyl = 4.110265665681823;", "r_cyl = 4.25;")
    assert "tyr337_chi2 = dihedral_gid" in emit_pot(spec)


def test_custom_group_pose_and_added_shape_match_emitted_program(structure):
    _, spec = imported()
    spec.core.indices = spec.core.indices[4:]
    spec.site.indices = spec.site.indices[:-4]
    spec.frame_ref.indices = spec.frame_ref.indices[4:]
    spec.origin_offset = 0.42
    spec.origin_lat1 = 0.27
    spec.origin_lat2 = -0.13
    spec.axis_tilt_deg = 6.0
    spec.axis_azimuth_deg = 37.0
    frame = source_frame(spec, structure)
    model = FunnelModel(structure, spec)
    probes = frame.cyl_to_world(np.array([0., 12., 26.]), np.array([2., 10., 8.]), np.array([0.4, 1.1, 2.2]))
    base_energy = np.asarray(model.total_field(probes))
    term = Term(var="adapter_probe", kind="sphere", radius=3.0,
                offset_e1=1.2, offset_e2=-0.7, center_z=2.0)
    expected_addition = term.energy(FieldCtx(model, points=probes))
    spec.terms.append(term)
    actual = evaluate_pot(emit_pot(spec), structure, probe=probes,
                          probe_group=spec.lig.var)
    np.testing.assert_allclose(actual.value.data[..., 0], base_energy + expected_addition, atol=1e-8)
    np.testing.assert_allclose(model.total_field(probes), actual.value.data[..., 0], atol=1e-8)
    np.testing.assert_allclose(actual.array("axis"), frame.axis, atol=1e-12)
    np.testing.assert_allclose(actual.array("origin"), frame.origin, atol=1e-12)


def test_custom_diagnostic_and_print_edits_preserve_complex_prints(structure):
    source, spec = imported()
    spec.diagnostics.append(Diagnostic("new_probe", "New probe", [3037, 3038]))
    spec.prints = [p for p in spec.prints if p[0] != "funnel_radius_A"]
    rendered = emit_pot(spec)
    assert 'print("frame_condition_sine",v_len/v_probe_len);' in rendered
    assert 'print("frame_anchor_cosine",v_par/v_probe_len);' in rendered
    assert 'print("funnel_radius_A",r_allowed);' not in rendered
    result = evaluate_pot(rendered, structure)
    assert "New probe_to_ligCOM_A" in result.printed()
    assert "Tyr337_chi1_cos_sin" in result.printed()
    assert "track_Asp74" in rendered and "track_Asp74" in source


def test_added_shape_can_be_reopened_edited_and_removed(structure):
    _, spec = imported()
    spec.terms.append(Term(var="new_shape", kind="sphere", radius=1.0))
    spec, report = parse_pot(emit_pot(spec))
    assert report.ok
    spec.terms[0].radius = 0.5
    small = evaluate_pot(emit_pot(spec), structure).value.data.copy()
    spec.terms.clear()
    rendered = emit_pot(spec)
    removed = evaluate_pot(rendered, structure).value.data
    assert float(small[0]) > float(removed[0])
    assert "@ff-term" not in rendered


def test_general_names_preserve_asymmetric_axial_force_constants():
    source = (Path(__file__).parent / "fixtures" / "funnel_general.pot").read_text()
    spec, report = parse_pot(source)
    assert report.ok
    assert spec.source_adapter["scalars"]["k_z"] == ["k_floor", "k_roof"]
    np.testing.assert_allclose(source_radius(spec, np.array([0., 25.])), [3.25 + 22.5 * 0.5773502691896258, 3.25])
    np.testing.assert_allclose(source_axial(spec, np.array([-3.5, 32.])), [20., 30.])
    spec.k_z = 80.0
    np.testing.assert_allclose(source_axial(spec, np.array([-3.5, 32.])), [40., 60.])
    assert "k_roof = 120.0;" in emit_pot(spec)


def test_cv_preview_retains_custom_radial_regularisation(structure):
    source, _ = imported()
    spec, report = parse_pot(source.replace("norm2(perp)+1.0e-12", "norm2(perp)+0.04"))
    assert report.ok
    frame = source_frame(spec, structure)
    z, rho = source_cvs(spec, structure, np.array([frame.origin, frame.origin + 2 * frame.axis]))
    np.testing.assert_allclose(z, [0., 2.], atol=1e-10)
    np.testing.assert_allclose(rho, [0.2, 0.2], atol=1e-10)


def test_custom_cylindrical_cv_dimension_changes_all_meta_arrays(structure):
    source, spec = imported()
    spec.dimension = 2
    spec.sigma_rho = 0.7
    rendered = emit_pot(spec)
    assert "v_old = meta(0,array(0.0,0.0,0.0),array(z,rho));" in rendered
    assert "v_meta = meta(0,array(hill,sigma_z,0.7),array(z,rho));" in rendered
    evaluate_pot(rendered, structure)
    spec.dimension = 1
    spec.sigma_rho = spec.source_adapter["baseline"]["sigma_rho"]
    assert emit_pot(spec) == source


def test_constant_energy_is_broadcast_over_probe_points(structure):
    source, _ = imported()
    spec, report = parse_pot(source.replace("v_total = v_rad+v_z+v_meta;", "v_total = 3.5;"))
    assert report.ok
    frame = source_frame(spec, structure)
    points = np.array([frame.origin, frame.origin + frame.axis])
    np.testing.assert_array_equal(source_field(spec, structure, points), [3.5, 3.5])


@pytest.mark.parametrize("selection", ["atom. 1,nope,3", "atom. 5-2", "atom. 0,2", "atom. -1,2"])
def test_bad_explicit_atom_selections_never_partially_resolve(selection):
    with pytest.raises(ValueError):
        parse_asl_atoms(selection)


@pytest.mark.parametrize("prefix", ["atom. ", "atom.num ", "a. ", "a.num ", "atom "])
def test_explicit_atom_aliases(prefix):
    assert parse_asl_atoms(prefix + "1,3-5") == [1, 3, 4, 5]
