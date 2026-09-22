"""The real custom-frame potential through the unchanged designer controls."""
import os

import numpy as np
import pytest

if not os.environ.get("DISPLAY"):
    pytest.skip("requires X; run with xvfb-run", allow_module_level=True)

from PyQt5 import QtCore, QtWidgets
from funnelforge.gui.main_window import MainWindow
from funnelforge.core.project import Job
from tests.fixture_paths import real_pot


@pytest.fixture
def custom_window(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *args, **kw: errors.append(str(args[2])))
    window = MainWindow()
    monkeypatch.setattr(window, "_import_report", lambda *args: None)
    window.job.import_bundle(real_pot())
    window.after_import()
    window.resize(1720, 990)
    window.show()
    app.processEvents()
    yield window, app, errors
    window.close()
    app.processEvents()


def test_custom_frame_controls_render_and_reopen(custom_window, tmp_path):
    window, app, errors = custom_window
    original = window.job.spec.source_text
    frame = window.job.model.frame
    origin, axis = frame.origin.copy(), frame.axis.copy()
    assert window.job.pot_text() == original
    assert window.viewer.funnel.counts["funnel"] > 0
    assert len(window.viewer.funnel.handles) >= 6
    window.funnel.origin_lat1.spin.setValue(0.02)
    window.funnel.axis_tilt.spin.setValue(0.1)
    window.funnel.k_rad.spin.setValue(30.0)
    app.processEvents()
    assert not errors, errors
    assert not np.allclose(origin, window.job.model.frame.origin)
    assert not np.allclose(axis, window.job.model.frame.axis)
    assert window.job.spec.k_rad == 30.0
    text = window.job.pot_text()
    assert 'print("Tyr337_chi1_cos_sin",tyr337_chi1);' in text
    assert window.job.verify_pot_text(text)["ok"]
    for index in range(4):
        window.plots.setCurrentIndex(index)
        window.plots.refresh()
        app.processEvents()
    path = tmp_path / "custom.pot"
    path.write_text(text)
    reopened = Job()
    reopened.import_bundle(str(path))
    reopened.attach_structure(window.job.paths.cms)
    np.testing.assert_allclose(reopened.model.frame.origin,
                               window.job.model.frame.origin, atol=1e-10)
    np.testing.assert_allclose(reopened.model.frame.axis,
                               window.job.model.frame.axis, atol=1e-10)
    assert reopened.pot_text() == text


def test_custom_field_layers_use_source_energy(custom_window):
    window, app, errors = custom_window
    style = window.viewer.funnel.style
    style.shell_mode = "energy"
    style.field_grid = 32
    style.show_section = True
    window.viewer.rebuild_funnel()
    app.processEvents()
    assert window.viewer.funnel.counts["funnel"] > 0
    assert not errors, errors


def test_unsupported_custom_edit_is_reported_and_reverted(custom_window):
    window, app, errors = custom_window
    before = window.job.pot_text()
    window.job.spec.dimension = 3
    window.on_changed()
    assert errors and "dimension" in errors[-1].lower()
    assert window.job.spec.dimension == 1
    assert window.job.pot_text() == before
