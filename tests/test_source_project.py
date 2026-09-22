"""Custom source import state, verification and persisted launch settings."""
import json
from pathlib import Path

import numpy as np
import pytest

from funnelforge.core.project import Job
from tests.fixture_paths import real_pot


def test_failed_import_preserves_the_open_job(tmp_path):
    job = Job()
    job.import_bundle(real_pot())
    structure, spec, paths = job.structure, job.spec, job.paths
    with pytest.raises(FileNotFoundError):
        job.import_bundle(str(tmp_path / "missing.pot"))
    assert job.structure is structure and job.spec is spec and job.paths is paths
    assert job.verify_pot_text(job.pot_text())["ok"]


def test_new_potential_only_import_does_not_reuse_previous_cms(tmp_path):
    job = Job()
    job.import_bundle(real_pot())
    path = tmp_path / "standalone.pot"
    path.write_text(job.pot_text())
    job.import_bundle(str(path))
    assert job.structure is None and job.model is None
    assert job.paths.cms is None or not job.paths.cms
    assert not job.verify_pot_text(job.pot_text())["ok"]


def test_session_keeps_custom_pose_and_job_file_changes(tmp_path):
    job = Job()
    job.import_bundle(real_pot())
    job.spec.origin_lat1 = 0.032345
    job.spec.axis_tilt_deg = 0.123456
    job.cfg.set_number("time", 123456.0)
    job.sh.host = "gpu-test"
    job.refresh_model()
    path = tmp_path / "session.json"
    job.save_session(str(path))
    loaded = Job()
    loaded.load_session(str(path))
    assert loaded.cfg.number("time") == 123456.0
    assert loaded.sh.host == "gpu-test"
    assert loaded.pot_text() == job.pot_text()
    np.testing.assert_allclose(loaded.model.frame.origin, job.model.frame.origin)


def test_verifier_rejects_changed_custom_physics_even_at_free_start():
    job = Job()
    job.import_bundle(real_pot())
    changed = job.pot_text().replace("0.5*k_rad*rad_excess^2", "0.51*k_rad*rad_excess^2")
    assert changed != job.pot_text()
    result = job.verify_pot_text(changed)
    assert not result["ok"]
    assert result["field_error_rel"] > 0.001
