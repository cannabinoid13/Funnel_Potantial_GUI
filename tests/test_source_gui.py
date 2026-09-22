"""End-to-end tests for the .pot source workbench.

Driven without a mouse, under a virtual display::

    xvfb-run -a python tests/test_source_gui.py

These cover the interactive promises: the two views stay in step, a
diagnostic navigates to its exact span, a slow answer never overwrites newer
text, saving is byte-identical, and a refused export writes nothing.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    print("no X display; run this file under xvfb-run")
    raise SystemExit(2)

from PyQt5 import QtCore, QtWidgets

QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts, True)
_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from funnelforge.core import safety
from funnelforge.core.lang import diagnostics as diag
from funnelforge.gui.source_window import SourceWindow

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, HERE)
from fixture_paths import real_pot, real_cms       # noqa: E402

REAL_POT = real_pot()
REAL_CMS = real_cms(REAL_POT)
HAVE_REAL = bool(REAL_POT)

_win = None


def spin(seconds: float = 0.6) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        _app.processEvents()
        time.sleep(0.01)


def window(path: str = "") -> SourceWindow:
    global _win
    if _win is None:
        _win = SourceWindow("")
        _win.resize(1500, 900)
        _win.show()
        spin(0.3)
    if path:
        _win.open_path(path)
        spin(1.2)
    return _win


def layer_row(w: SourceWindow, needle: str) -> int:
    for r in range(w.layer_table.rowCount()):
        if needle in w.layer_table.item(r, 0).text():
            return r
    raise AssertionError(f"no layer row matching {needle}")


# ---------------------------------------------------------------- basics
def test_the_workbench_opens_a_potential_and_fills_every_view():
    if not HAVE_REAL:
        return
    w = window(REAL_POT)
    assert w.editor.toPlainText() == open(REAL_POT, encoding="utf-8").read()
    assert w.layer_table.rowCount() == len(diag.LAYERS)
    assert w.layer_table.item(layer_row(w, "Source syntax"), 1).text() == "PASS"
    tops = [w.tree.topLevelItem(i).text(0)
            for i in range(w.tree.topLevelItemCount())]
    assert tops == ["Declarations", "Atom selections", "Metadynamics",
                    "Applied potential", "Preserved but not modelled"], tops
    assert w.sym_box.count() > 10
    assert w.diag_list.topLevelItemCount() >= 1


def test_the_structure_view_names_the_probe_the_bias_and_the_walls():
    if not HAVE_REAL:
        return
    w = window(REAL_POT)
    mets = next(w.tree.topLevelItem(i)
                for i in range(w.tree.topLevelItemCount())
                if w.tree.topLevelItem(i).text(0) == "Metadynamics")
    rows = [(mets.child(i).text(0), mets.child(i).text(1))
            for i in range(mets.childCount())]
    assert any("probe" in d for _, d in rows)
    assert any("well-tempered" in d for _, d in rows)
    energy = next(w.tree.topLevelItem(i)
                  for i in range(w.tree.topLevelItemCount())
                  if w.tree.topLevelItem(i).text(0) == "Applied potential")
    labels = [energy.child(i).text(0) for i in range(energy.childCount())]
    assert any(l.startswith("v_rad") for l in labels), labels
    assert any(l.startswith("v_z") for l in labels), labels
    assert any(l.startswith("v_meta") for l in labels), labels


def test_a_diagnostic_navigates_to_its_exact_span():
    src = ('declare_output(name = "o", first = 0.0, interval = 1.0);\n'
           'a = 1.0;\n'
           'b = a + ghost;\n'
           'b;\n')
    w = window()
    w.editor.setPlainText(src)
    spin(1.0)
    items = [w.diag_list.topLevelItem(i)
             for i in range(w.diag_list.topLevelItemCount())]
    target = next(i for i in items if i.text(1) == "SEM001")
    w._goto_diagnostic(target)
    spin(0.2)
    c = w.editor.textCursor()
    assert c.selectedText() == "ghost", repr(c.selectedText())
    assert "SEM001" in w.diag_detail.toPlainText()
    assert "never defined" in w.diag_detail.toPlainText()


def test_clicking_the_structure_tree_selects_the_source():
    if not HAVE_REAL:
        return
    w = window(REAL_POT)
    sels = next(w.tree.topLevelItem(i)
                for i in range(w.tree.topLevelItemCount())
                if w.tree.topLevelItem(i).text(0) == "Atom selections")
    first = sels.child(0)
    w._goto_node(first)
    spin(0.2)
    picked = w.editor.textCursor().selectedText()
    assert picked.startswith(first.text(0)), (picked[:40], first.text(0))


def test_the_filter_narrows_the_diagnostic_list():
    if not HAVE_REAL:
        return
    w = window(REAL_POT)
    total = w.diag_list.topLevelItemCount()
    w.filter.setText("MTD")
    spin(0.2)
    narrowed = w.diag_list.topLevelItemCount()
    assert 0 < narrowed <= total
    for i in range(narrowed):
        assert "MTD" in w.diag_list.topLevelItem(i).text(1)
    w.filter.setText("zzzz-nothing")
    spin(0.2)
    assert w.diag_list.topLevelItemCount() == 0
    w.filter.setText("")
    spin(0.2)
    assert w.diag_list.topLevelItemCount() == total


# ---------------------------------------------------------------- revisions
def test_a_stale_worker_result_never_overwrites_newer_text():
    w = window()
    w.editor.setPlainText('declare_output(name = "o", first = 0.0, '
                          'interval = 1.0);\na = 1.0;\na;\n')
    spin(1.0)
    good = w.view_doc.rev
    # an answer that belongs to text nobody is looking at any more
    from funnelforge.core.document import Document
    stale = Document.from_text("this is not the current text")
    stale.run_local_layers()
    w._analysis_ready("a-revision-that-is-not-current", stale)
    assert w.view_doc.rev == good, "an old answer replaced the current view"


def test_editing_makes_the_official_verdict_stale_and_reblocks_export():
    if not HAVE_REAL:
        return
    w = window(REAL_POT)
    # pretend the engine accepted this revision, without paying for a run
    rev = w.doc.rev
    w.view_doc.report.mark("official", diag.State.PASSED,
                           note="pretend run", source_rev=rev)
    assert not [b for b in w.view_doc.export_blockers()
                if b.code.startswith("OFF")]
    w.editor.setPlainText(w.editor.toPlainText() + "\n")
    spin(1.2)
    assert w.layer_table.item(layer_row(w, "Desmond engine"), 1).text() \
        == "STALE"
    codes = {b.code for b in w.view_doc.export_blockers()}
    assert "OFF008" in codes, codes
    assert "out of date" in w.lbl_verdict.text().lower()


def test_the_verdict_never_says_valid_before_the_engine_has_run():
    if not HAVE_REAL:
        return
    w = window(REAL_POT)
    text = w.lbl_verdict.text().lower()
    assert "not performed" in text or "out of date" in text
    assert not text.startswith("desmond-valid")


# ---------------------------------------------------------------- writing
def test_saving_from_the_workbench_is_byte_for_byte_identical():
    tmp = tempfile.mkdtemp(prefix="ff_gui_rt_")
    for name in ("crlf.pot", "bom_utf8.pot", "no_final_newline.pot",
                 "unicode_comments.pot", "mixed_endings.pot"):
        src = os.path.join(FIXTURES, name)
        dst = os.path.join(tmp, name)
        shutil.copyfile(src, dst)
        w = window(dst)
        w._save_source()
        spin(0.2)
        assert open(dst, "rb").read() == open(src, "rb").read(), name
    shutil.rmtree(tmp, ignore_errors=True)


def test_mixed_line_endings_survive_unless_you_actually_edit():
    """Qt cannot hold CR and LF at once, so the contract is explicit.

    Untouched, the original bytes are written back exactly.  After a real
    edit the file is normalised to LF and the user is told, rather than the
    interface quietly claiming to have preserved something it did not.
    """
    tmp = tempfile.mkdtemp(prefix="ff_gui_mix_")
    src = os.path.join(FIXTURES, "mixed_endings.pot")
    dst = os.path.join(tmp, "mixed_endings.pot")
    shutil.copyfile(src, dst)
    raw = open(src, "rb").read()
    assert b"\r\n" in raw and raw.count(b"\r\n") < raw.count(b"\n")

    w = window(dst)
    w._save_source()
    spin(0.2)
    assert open(dst, "rb").read() == raw, "an untouched file must not change"

    w.editor.setPlainText(w.editor.toPlainText().replace("0.0", "0.5", 1))
    spin(0.8)
    w._save_source()
    spin(0.2)
    after = open(dst, "rb").read()
    assert after != raw and b"\r" not in after
    assert "line endings" in w.status.currentMessage().lower(), \
        w.status.currentMessage()
    shutil.rmtree(tmp, ignore_errors=True)


def test_a_refused_export_writes_nothing_but_saving_still_works():
    w = window()
    w.editor.setPlainText('declare_output(name = "o", first = 0.0, '
                          'interval = 1.0);\n'
                          'good = 1.0;\n'
                          'mystery @@ from the future;\n'
                          'total = good + mystery;\n'
                          'total;\n')
    spin(1.0)
    blockers = {b.code for b in w.view_doc.export_blockers()}
    assert "MOD004" in blockers, blockers
    tmp = tempfile.mkdtemp(prefix="ff_gui_exp_")
    target = os.path.join(tmp, "out.pot")
    written, why = w.doc.export_run_ready(target)
    assert written == "" and not os.path.exists(target)
    w.doc.save_source(target)
    assert open(target).read() == w.editor.toPlainText()
    shutil.rmtree(tmp, ignore_errors=True)


def test_the_diff_is_available_before_saving():
    tmp = tempfile.mkdtemp(prefix="ff_gui_diff_")
    p = os.path.join(tmp, "x.pot")
    open(p, "w").write('declare_output(name = "o", first = 0.0, '
                       'interval = 1.0);\na = 1.0;\na;\n')
    w = window(p)
    w.editor.setPlainText(w.editor.toPlainText().replace("1.0;", "2.0;", 1))
    spin(0.8)
    from funnelforge.core.lang import patch
    original, _ = safety.read_text_file(p)
    d = patch.diff_summary(original, w.editor.toPlainText())
    assert "-a = 1.0;" in d and "+a = 2.0;" in d, d
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- a11y
def test_the_interface_is_reachable_without_a_mouse_or_colour():
    w = window()
    for widget, name in ((w.editor, "Potential source text"),
                         (w.layer_table, "Validation layers"),
                         (w.diag_list, "Diagnostics"),
                         (w.tree, "Structure of the potential"),
                         (w.sel_table, "Atom selections"),
                         (w.filter, "Filter diagnostics")):
        assert widget.accessibleName() == name, widget.accessibleName()
    # every state is spelled out in text, not signalled by colour alone
    if HAVE_REAL:
        w2 = window(REAL_POT)
        for r in range(w2.layer_table.rowCount()):
            assert w2.layer_table.item(r, 1).text() in (
                "PASS", "WARN", "FAIL", "not run", "STALE")
    shortcuts = {a.shortcut().toString() for a in w.findChildren(
        QtWidgets.QAction)}
    for want in ("Ctrl+O", "Ctrl+S", "Ctrl+E", "Ctrl+D", "F5"):
        assert want in shortcuts, (want, sorted(shortcuts))


def test_the_engine_choice_defaults_to_the_newest_installation():
    from funnelforge.core.desmond import adapter
    w = window()
    preferred = adapter.default_installation()
    if preferred is None or not w.installations:
        return
    assert w.installations[w.inst_box.currentIndex()].path == preferred.path


def _main() -> int:
    tests = sorted(((k, v) for k, v in globals().items()
                    if k.startswith("test_") and callable(v)),
                   key=lambda kv: kv[1].__code__.co_firstlineno)
    fails = 0
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"  PASS  {name}  ({time.time() - t0:.2f}s)")
        except Exception as exc:
            fails += 1
            import traceback
            print(f"  FAIL  {name}: {exc}")
            traceback.print_exc(limit=3)
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_main())
