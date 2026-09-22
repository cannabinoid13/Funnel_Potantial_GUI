"""Tests for the lossless .pot front end: lexer, CST, semantics, document.

These are the regression contract for the correctness requirements:
nothing is dropped, nothing is renamed, no fixed variable names are needed,
every bias and wall that reaches the potential is found, unknown syntax
survives, and the interface never calls anything Desmond-valid on its own
authority.

Run::

    python tests/test_lang.py
    FUNNELFORGE_OFFICIAL=1 python tests/test_lang.py   # adds the engine tier
"""

from __future__ import annotations

import io
import json
import os
import random
import shutil
import string
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from funnelforge.core import safety
from funnelforge.core.document import Document
from funnelforge.core.lang import cst, patch, registry, sema
from funnelforge.core.lang import diagnostics as diag
from funnelforge.core.lang.lexer import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture_paths import real_pot, real_cms       # noqa: E402

#: A real production potential from the repository's own corpus - a different
#: molecular system, written by a different toolkit, using none of this
#: program's naming conventions.  That is the point: these tests must hold for
#: any Desmond potential, so the fixture is deliberately not one of ours.
REAL_POT = real_pot()
REAL_CMS = real_cms(REAL_POT)
HAVE_REAL = bool(REAL_POT)


def fixtures() -> list:
    return sorted(os.path.join(FIXTURES, f) for f in os.listdir(FIXTURES)
                  if f.endswith(".pot"))


def corpus() -> list:
    out = fixtures()
    if HAVE_REAL:
        out.append(REAL_POT)
    return out


# ---------------------------------------------------------------- lexer
def test_the_lexer_loses_nothing():
    for path in corpus():
        raw = open(path, "rb").read()
        text, enc = safety.read_text_file(path)
        res = tokenize(text)
        assert res.text == text, path
        assert enc.encode(text) == raw, f"encoding is not an inverse: {path}"


def test_line_endings_and_bom_are_preserved_exactly():
    cases = [
        b"a = 1.0;\nb = 2.0;\n",
        b"a = 1.0;\r\nb = 2.0;\r\n",
        b"\xef\xbb\xbfa = 1.0;\n",
        b"a = 1.0;\nb = 2.0;",              # no final newline
        b"a = 1.0;\nb = 2.0;\r\nc = 3.0;\n",  # mixed
    ]
    for raw in cases:
        enc = safety.sniff_encoding(raw)
        text = enc.decode(raw)
        assert enc.encode(text) == raw, raw
        assert tokenize(text).text == text


# ---------------------------------------------------------------- CST
def test_every_fixture_parses_and_nothing_is_orphaned():
    for path in corpus():
        text, _ = safety.read_text_file(path)
        res = cst.parse(text)
        assert not cst.coverage_gaps(res), \
            f"{path}: tokens claimed by no statement"
        for node in res.program.walk():
            assert 0 <= node.start <= node.end <= len(text)


def test_unknown_syntax_survives_as_an_opaque_span():
    src = ('declare_output(name = "o", first = 0.0, interval = 1.0);\n'
           'a = 1.0;\n'
           'weird @@@ construct from the future;\n'
           'b = a + 1.0;\n'
           'b;\n')
    res = cst.parse(src)
    op = res.opaque_nodes
    assert len(op) == 1, [n.span_text(src) for n in op]
    assert "weird" in op[0].span_text(src)
    assert res.problems
    # and the rest of the file still parsed
    kinds = [s.kind for s in res.statements()]
    assert kinds.count(cst.BIND) == 2
    # the text is untouched
    doc = Document.from_text(src)
    assert doc.source == src


def test_unknown_functions_are_preserved_not_rejected():
    path = os.path.join(FIXTURES, "future_function.pot")
    text, _ = safety.read_text_file(path)
    doc = Document.from_text(text, path)
    an = doc.analysis()
    assert an.unknown_functions, "a future function should be flagged"
    assert not doc.parsed().opaque_nodes, \
        "an unknown *function* is ordinary syntax and must not go opaque"
    doc.run_local_layers()
    codes = {d.code for d in doc.report.all()}
    assert "MOD003" in codes
    assert doc.report.state_of("syntax") is diag.State.PASSED
    # preserved verbatim
    assert doc.source == text


def test_operator_precedence_matches_the_grammar():
    #  -x^2 is -(x^2), and ^ is right associative with a signed exponent
    res = cst.parse("y = -x^2;\nz = 2.0^-3.0^2.0;\ny;\n")
    y = res.statements()[0].children[0]
    assert y.kind == cst.UNARY and y.op == "-"
    assert y.children[0].kind == cst.BINARY and y.children[0].op == "^"
    z = res.statements()[1].children[0]
    assert z.kind == cst.BINARY and z.op == "^"
    assert z.children[1].kind == cst.UNARY          # right associative


def test_a_comment_or_string_never_creates_a_meta_call():
    path = os.path.join(FIXTURES, "meta_in_string.pot")
    text, _ = safety.read_text_file(path)
    an = sema.analyse(cst.parse(text))
    for m in an.metas:
        assert m.node.kind == cst.CALL and m.node.name == "meta"
    # the words are in the file but they are not calls
    assert "meta(" in text
    real = [n for n in cst.parse(text).program.walk()
            if n.kind == cst.CALL and n.name == "meta"]
    assert len(an.metas) == len(real)


# ---------------------------------------------------------------- semantics
def test_no_fixed_names_are_required():
    path = os.path.join(FIXTURES, "arbitrary_names.pot")
    text, _ = safety.read_text_file(path)
    for banned in ("lig", "site", "core", "v_total", "v_meta", "cv"):
        assert f"\n{banned} " not in text and f"\n{banned}=" not in text
    doc = Document.from_text(text, path)
    an = doc.analysis()
    assert an.energy_stmt is not None
    assert an.metas, "the bias must be found without knowing any names"
    assert any(m.wt and m.wt.confirmed for m in an.metas)
    assert an.contributions


def test_nested_and_aliased_meta_calls_are_all_found():
    text, _ = safety.read_text_file(os.path.join(FIXTURES, "nested_meta.pot"))
    res = cst.parse(text)
    an = sema.analyse(res)
    # count real call nodes, not substrings: this fixture mentions "meta("
    # seven more times inside its own comments precisely to catch a scanner
    real = [n for n in res.program.walk()
            if n.kind == cst.CALL and n.name == "meta"]
    assert len(real) == 6, len(real)
    assert len(an.metas) == len(real), (len(an.metas), len(real))
    assert any(m.depth > 3 for m in an.metas), \
        "a call nested inside an argument must still be found"
    # one of them is only ever printed, so it is a diagnostic, not a bias
    assert any(m.role == sema.DIAGNOSTIC for m in an.metas)


def test_probe_and_bias_are_distinguished_and_wt_is_proved():
    text, _ = safety.read_text_file(os.path.join(FIXTURES, "metad_2d_wt.pot"))
    an = sema.analyse(cst.parse(text))
    roles = sorted(m.role for m in an.metas)
    assert roles == [sema.BIAS, sema.PROBE], roles
    bias = next(m for m in an.metas if m.role == sema.BIAS)
    assert bias.wt is not None and bias.wt.confirmed
    # kDT = (gamma-1) kB T = 14 * 0.0019872041 * 310
    assert abs(bias.wt.ktemp - 14 * 0.0019872041 * 310) < 1e-9
    assert abs(bias.wt.h0 - 0.10) < 1e-12
    assert bias.wt.probe is not None and bias.wt.probe.zero_height


def test_plain_metad_is_not_reported_as_well_tempered():
    text, _ = safety.read_text_file(os.path.join(FIXTURES, "metad_1d.pot"))
    an = sema.analyse(cst.parse(text))
    assert an.metas
    for m in an.metas:
        assert not (m.wt and m.wt.confirmed)
        if m.role != sema.PROBE:
            assert m.wt is not None and m.wt.evidence


def test_multiple_accumulators_are_judged_separately():
    text, _ = safety.read_text_file(
        os.path.join(FIXTURES, "multi_accumulator.pot"))
    an = sema.analyse(cst.parse(text))
    assert len(an.meta_declarations) >= 2
    wt = {m.index: bool(m.wt and m.wt.confirmed)
          for m in an.metas if m.role != sema.PROBE}
    assert wt.get(0) is True and wt.get(1) is False, wt


def test_every_contribution_to_the_energy_is_traceable():
    if not HAVE_REAL:
        return
    text, _ = safety.read_text_file(REAL_POT)
    an = sema.analyse(cst.parse(text))
    labels = [c.label.split()[0] for c in an.contributions]
    assert labels == ["v_rad", "v_z", "v_meta"], labels
    assert not any(c.opaque for c in an.contributions)
    assert all(c.symbol is not None for c in an.contributions)


def test_a_selection_is_not_unused_when_it_only_feeds_a_print():
    src = ('declare_output(name = "o", first = 0.0, interval = 1.0);\n'
           'g = atomsel("atom. 1,2,3");\n'
           'c = center_of_mass(g);\n'
           'd = norm(c);\n'
           'print("d", d);\n'
           'e = 1.0;\n'
           'e;\n')
    an = sema.analyse(cst.parse(src))
    assert an.symbols["g"].reaches_side_effect
    assert not an.symbols["g"].reaches_energy
    assert an.symbols["e"].reaches_energy


def test_the_diagnosed_problems_are_the_intended_ones():
    want = {
        "undefined_symbol.pot": "SEM001",
        "duplicate_assign.pot": "SEM002",
        "dependency_cycle.pot": "SEM003",
        "bad_accumulator.pot": "MTD003",
        "dimension_mismatch.pot": "MTD004",
    }
    for name, code in want.items():
        text, _ = safety.read_text_file(os.path.join(FIXTURES, name))
        an = sema.analyse(cst.parse(text))
        assert code in {p.code for p in an.problems}, (name, code,
                                                       [p.code for p in
                                                        an.problems])


def test_clean_fixtures_report_nothing():
    clean = ["minimal.pot", "no_metad.pot", "walls_only.pot",
             "funnel_general.pot", "metad_2d_wt.pot", "ifelse_chains.pot",
             "blocks_series_static.pot", "arrays_indexing.pot",
             "scientific_numbers.pot", "multiline_calls.pot"]
    for name in clean:
        text, _ = safety.read_text_file(os.path.join(FIXTURES, name))
        res = cst.parse(text)
        an = sema.analyse(res)
        errs = [p.code for p in an.problems]
        assert not errs, (name, errs)


def test_the_engines_grammar_rules_are_enforced_before_the_engine():
    # a declaration after the body: the engine says "no viable alternative
    # at input 'static'"
    res = cst.parse('a = 1.0;\nstatic keeper(1);\na;\n')
    assert any(p.code == "SYN001" and "header" in p.message
               for p in res.problems)
    # a comment with no newline after it: the engine says "no viable
    # alternative at character '<EOF>'"
    res = cst.parse('a = 1.0;\na;\n# trailing')
    assert any(p.code == "SYN006" for p in res.problems)


# ---------------------------------------------------------------- editing
def test_a_structured_edit_touches_only_its_own_span():
    for path in corpus():
        text, _ = safety.read_text_file(path)
        doc = Document.from_text(text, path)
        nums = [n for n in doc.parsed().program.walk() if n.kind == cst.NUM]
        if not nums:
            continue
        target = nums[len(nums) // 2]
        edit = patch.replace_node(target, "42.5", "test")
        before = doc.source
        doc.apply_edits([edit])
        assert doc.source[:target.start] == before[:target.start]
        assert doc.source[target.start + 4:] == before[target.end:]
        assert patch.unchanged_outside(before, doc.source, [edit])
        assert doc.undo() and doc.source == before


def test_overlapping_edits_are_refused():
    src = "a = 1.0;\na;\n"
    try:
        patch.apply(src, [patch.Edit(0, 5, "x"), patch.Edit(3, 8, "y")])
    except patch.OverlappingEdits:
        pass
    else:
        raise AssertionError("overlapping edits must not be applied")


# ---------------------------------------------------------------- document
def test_open_and_save_is_byte_for_byte_identical():
    tmp = tempfile.mkdtemp(prefix="ff_rt_")
    for path in corpus():
        raw = open(path, "rb").read()
        copy = os.path.join(tmp, os.path.basename(path))
        with open(copy, "wb") as fh:
            fh.write(raw)
        doc = Document.open(copy)
        doc.save_source()
        assert open(copy, "rb").read() == raw, path
    shutil.rmtree(tmp, ignore_errors=True)


def test_saving_detects_a_file_that_changed_underneath():
    tmp = tempfile.mkdtemp(prefix="ff_ext_")
    p = os.path.join(tmp, "x.pot")
    open(p, "w").write('a = 1.0;\na;\n')
    doc = Document.open(p)
    doc.set_source('a = 2.0;\na;\n')
    open(p, "w").write("someone else wrote this\n")
    assert doc.external_change()
    try:
        doc.save_source()
    except safety.ExternalChange:
        pass
    else:
        raise AssertionError("a changed file must not be clobbered silently")
    assert open(p).read() == "someone else wrote this\n"
    doc.save_source(allow_overwrite_changed=True)
    assert open(p).read() == 'a = 2.0;\na;\n'
    shutil.rmtree(tmp, ignore_errors=True)


def test_the_interface_never_claims_desmond_valid_on_its_own():
    if not HAVE_REAL:
        return
    doc = Document.open(REAL_POT)
    doc.run_local_layers()
    assert doc.report.state_of("official") is diag.State.NOT_RUN
    v = doc.verdict()
    assert "not performed" in v.lower(), v
    assert "desmond-valid" not in v.lower()
    blockers = {b.code for b in doc.export_blockers()}
    assert "OFF001" in blockers


def test_a_lossy_export_is_blocked_before_anything_is_written():
    src = ('declare_output(name = "o", first = 0.0, interval = 1.0);\n'
           'good = 1.0;\n'
           'mystery @@ from the future;\n'
           'total = good + mystery;\n'
           'total;\n')
    doc = Document.from_text(src)
    doc.run_local_layers()
    assert doc.analysis().export_is_lossy()
    tmp = tempfile.mkdtemp(prefix="ff_exp_")
    target = os.path.join(tmp, "out.pot")
    written, blockers = doc.export_run_ready(target)
    assert written == ""
    assert not os.path.exists(target), "a refused export must write nothing"
    assert "MOD004" in {b.code for b in blockers}
    # the lossless path is still open
    doc.save_source(target)
    assert open(target).read() == src
    shutil.rmtree(tmp, ignore_errors=True)


def test_results_go_stale_when_the_text_changes():
    doc = Document.from_text('declare_output(name = "o", first = 0.0, '
                             'interval = 1.0);\na = 1.0;\na;\n')
    doc.run_local_layers()
    rev = doc.rev
    assert doc.report.layers["syntax"].source_rev == rev
    assert doc.report.state_of("syntax") is diag.State.PASSED
    doc.set_source(doc.source.replace("1.0;", "2.0;"))
    assert doc.report.state_of("syntax") is diag.State.STALE
    doc.run_local_layers()
    assert doc.report.state_of("syntax") is diag.State.PASSED
    assert doc.report.layers["syntax"].source_rev == doc.rev


def test_selections_resolve_against_a_real_structure():
    if not (HAVE_REAL and os.path.exists(REAL_CMS)):
        return
    doc = Document.open(REAL_POT)
    doc.run_local_layers()
    table = doc.resolve_topology(REAL_CMS)
    assert table, "the fixture declares atom selections"
    from funnelforge.core.cms import Structure
    st = Structure.load(REAL_CMS)
    for row in table:
        assert row["n"] is not None, (row["name"], row["note"])
        assert row["n"] > 0
        # the count is the count of the indices the ASL names, and every one
        # of them is a real atom of this structure
        assert row["chains"] and all(c for c in row["chains"])
        assert row["residues"]
    total = sum(r["n"] for r in table)
    assert 0 < total <= st.n_atoms
    assert doc.report.state_of("topology") is diag.State.PASSED


def test_the_selection_reader_never_guesses_at_what_it_cannot_read():
    """A partly-read selection is worse than an unread one.

    ``parse_asl_atoms`` skips pieces it cannot parse, so ``atom. 1,2,foo``
    used to resolve to two atoms and look authoritative.  Only the spellings
    that really mean "these indices" are accepted; anything else is refused
    and reported as a form this interface does not read.
    """
    if not (HAVE_REAL and os.path.exists(REAL_CMS)):
        return
    from funnelforge.core import asl as asl_mod
    from funnelforge.core.cms import Structure
    from funnelforge.core.document import _resolve_asl
    st = Structure.load(REAL_CMS)
    # a range of real atom indices in this structure, whatever it is
    a = st.n_atoms // 3
    b = a + 71
    for text, n in ((f"atom. {a}-{b}", 72), (f"atom.num {a}-{b}", 72),
                    ("a.n 1,2,3", 3), ("atom.n 5-9", 5), ("atom 1,2,3", 3)):
        assert len(_resolve_asl(text, st, asl_mod)) == n, text
    for text in ("atom. 1,2,foo", 'atom.ptype " CA "', "atom.element C",
                 "atom. "):
        try:
            got = _resolve_asl(text, st, asl_mod)
        except Exception:
            continue
        raise AssertionError(f"{text!r} was read as {len(got)} atoms instead "
                             "of being refused")


def test_an_empty_selection_is_an_error_not_a_crash():
    if not (HAVE_REAL and os.path.exists(REAL_CMS)):
        return
    src = ('declare_output(name = "o", first = 0.0, interval = 1.0);\n'
           'g = atomsel("atom. 999999");\n'
           'c = center_of_mass(g);\n'
           'norm(c);\n')
    doc = Document.from_text(src)
    doc.run_local_layers()
    doc.resolve_topology(REAL_CMS)
    codes = {d.code for d in doc.report.layers["topology"].diagnostics}
    assert codes & {"TOP001", "TOP002"}, codes


def test_the_package_layer_reads_the_companion_files():
    if not HAVE_REAL:
        return
    doc = Document.open(REAL_POT)
    doc.run_local_layers()
    st = doc.check_package()
    assert st.state is diag.State.PASSED, [d.message for d in st.diagnostics]
    for want in (".msj", ".cfg", ".sh", ".cms"):
        assert want in st.note, st.note


def test_the_package_layer_catches_a_broken_job():
    if not HAVE_REAL:
        return
    import re as _re
    base = os.path.splitext(REAL_POT)[0]
    tmp = tempfile.mkdtemp(prefix="ff_pkg_")

    # a potential renamed away from the job its .msj loads
    for ext in (".pot", ".msj", ".cfg", ".sh"):
        if os.path.exists(base + ext):
            shutil.copyfile(base + ext, os.path.join(tmp, "renamed" + ext))
    doc = Document.open(os.path.join(tmp, "renamed.pot"))
    doc.run_local_layers()
    codes = {d.code for d in doc.check_package().diagnostics}
    assert "PKG002" in codes, codes

    # a potential that restarts from a kernel file that is not there
    q = os.path.join(tmp, "restart.pot")
    text, _ = safety.read_text_file(REAL_POT)
    restarted, n = _re.subn(r'initial\s*=\s*"[^"]*"',
                            'initial = "gone.kerseq"', text, count=1)
    assert n == 1, "the fixture should declare an initial kernel"
    with open(q, "w") as fh:
        fh.write(restarted)
    doc2 = Document.open(q)
    doc2.run_local_layers()
    st = doc2.check_package()
    assert "PKG009" in {d.code for d in st.diagnostics}
    # and it goes quiet once the kernel is really there
    open(os.path.join(tmp, "gone.kerseq"), "w").close()
    doc3 = Document.open(q)
    doc3.run_local_layers()
    assert "PKG009" not in {d.code for d in doc3.check_package().diagnostics}

    # a production chain that never switches the bias on
    m = os.path.join(tmp, "nobias.msj")
    msj, _ = safety.read_text_file(base + ".msj")
    # whatever spacing the producing toolkit used
    off, n = _re.subn(r"(\bmeta\s*=\s*)FILE", r"\1none", msj)
    assert n >= 1, "the fixture's .msj should switch the bias on"
    with open(m, "w") as fh:
        fh.write(off)
    shutil.copyfile(REAL_POT, os.path.join(tmp, "nobias.pot"))
    doc4 = Document.open(os.path.join(tmp, "nobias.pot"))
    doc4.run_local_layers()
    assert "PKG008" in {d.code for d in doc4.check_package().diagnostics}
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- safety
def test_archive_extraction_refuses_hostile_members():
    tmp = tempfile.mkdtemp(prefix="ff_zip_")
    bad = os.path.join(tmp, "bad.zip")
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../escape.pot", "a;\n")
    dest = os.path.join(tmp, "out")
    os.makedirs(dest, exist_ok=True)
    try:
        safety.safe_extract_zip(bad, dest)
    except Exception:
        pass
    else:
        raise AssertionError("path traversal must be refused")
    assert not os.path.exists(os.path.join(tmp, "escape.pot"))
    shutil.rmtree(tmp, ignore_errors=True)


def test_a_file_over_the_limit_is_refused_rather_than_read():
    tmp = tempfile.mkdtemp(prefix="ff_big_")
    p = os.path.join(tmp, "big.pot")
    open(p, "w").write("a = 1.0;\n" * 1000)
    try:
        safety.read_text_file(p, safety.Limits(max_file_bytes=100))
    except safety.LimitExceeded:
        pass
    else:
        raise AssertionError("the size limit must be enforced")
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- fuzz
def test_fuzzing_never_crashes_hangs_or_loses_text():
    rng = random.Random(20260828)
    seeds = [open(p, encoding="utf-8", errors="replace").read()
             for p in fixtures()[:12]]
    alphabet = list('(){}[];,=+-*/^"#\n\t abcXYZ0123456789.eE:') + \
        ["series", "if", "then", "else", "static", "meta", "atomsel", "å"]
    for i in range(400):
        base = rng.choice(seeds)
        s = list(base)
        for _ in range(rng.randint(1, 12)):
            op = rng.random()
            if op < 0.4 and s:
                del s[rng.randrange(len(s))]
            elif op < 0.8:
                s.insert(rng.randrange(len(s) + 1), rng.choice(alphabet))
            elif s:
                a = rng.randrange(len(s))
                b = min(len(s), a + rng.randint(1, 40))
                chunk = s[a:b]
                s[a:b] = list(reversed(chunk))
        text = "".join(s)
        t0 = time.time()
        res = cst.parse(text, max_depth=128)
        assert tokenize(text).text == text, "the lexer dropped text"
        sema.analyse(res)
        assert time.time() - t0 < 10.0, f"iteration {i} took too long"
        doc = Document.from_text(text)
        doc.run_local_layers()
        assert doc.source == text, "the document mutated its own source"


def test_a_long_flat_sum_does_not_blow_the_stack():
    """A potential that adds up many walls is ordinary, and used to crash.

    The parser's depth guard counts parser recursion, which a left-
    associative chain never raises: ``a+b+c+...`` is one loop, but the tree
    it builds is as deep as the sum is long.  At 497 terms the semantic pass
    hit CPython's own limit and raised RecursionError out of
    ``run_local_layers``.
    """
    head = 'declare_output(name = "o", first = 0.0, interval = 1.0);\n'

    def build(n):
        return (head + "".join(f"w{i} = 1.0;\n" for i in range(n))
                + "v = " + " + ".join(f"w{i}" for i in range(n)) + ";\nv;\n")

    for n in (497, 1200):
        doc = Document.from_text(build(n))
        doc.run_local_layers()                     # must not raise
        an = doc.analysis()
        assert len(an.contributions) == n, (n, len(an.contributions))
        assert not [p for p in an.problems if p.code.startswith("SYN")]
        assert doc.source == build(n)

    # walk() itself must be iterative, not merely lucky
    res = cst.parse(build(3000))
    assert len(list(res.program.walk())) > 3000
    assert res.program.tree_depth() > 3000

    # and past the analysis ceiling it declines instead of crashing
    huge = Document.from_text(build(sema.MAX_TREE_DEPTH + 50))
    huge.run_local_layers()
    assert "SYN010" in {d.code for d in huge.report.all()}
    assert huge.source.startswith(head)


def test_deeply_nested_input_is_bounded_not_fatal():
    src = "a = " + "(" * 5000 + "1.0" + ")" * 5000 + ";\na;\n"
    res = cst.parse(src, max_depth=64)
    assert res.problems
    assert tokenize(src).text == src
    doc = Document.from_text(src)
    doc.run_local_layers()
    assert doc.source == src


# ---------------------------------------------------------------- performance
def test_parsing_and_analysis_stay_fast_enough_to_type_against():
    if not HAVE_REAL:
        return
    text, _ = safety.read_text_file(REAL_POT)
    big = text * 40                       # ~140 kB, ~2200 statements
    t0 = time.time()
    res = cst.parse(big)
    t_parse = time.time() - t0
    t0 = time.time()
    sema.analyse(res)
    t_sema = time.time() - t0
    assert t_parse < 3.0, f"parse took {t_parse:.2f}s"
    assert t_sema < 20.0, f"analysis took {t_sema:.2f}s"
    t0 = time.time()
    cst.parse(text)
    assert time.time() - t0 < 0.25


def test_registry_knows_the_installed_release_and_refuses_to_guess():
    sigs = registry.signatures()
    for must in ("atomsel", "center_of_mass", "meta", "min_image", "dot",
                 "norm", "series" if "series" in sigs else "pos"):
        assert must in sigs or must in registry.RELEASES[
            registry.DEFAULT_RELEASE].keywords, must
    assert registry.get("quantum_cv") is None
    assert not registry.known("quantum_cv")
    assert registry.is_side_effecting("print")
    rows = registry.capability_matrix()
    assert rows and any(r.get("validated") for r in rows)


# ---------------------------------------------------------------- official
def test_a_declaration_term_the_grammar_forbids_is_reported():
    """mexp.g gives each header form a closed set of terms."""
    res = cst.parse('declare_output(name = "o", cutoff = 9.0, first = 0.0, '
                    'interval = 1.0);\na = 1.0;\na;\n')
    assert any(p.code == "SYN001" and "cutoff" in p.message
               for p in res.problems), [p.message for p in res.problems]
    # declare_meta does take cutoff, and must not be flagged for it
    ok = cst.parse('declare_meta(dimension = 1, cutoff = 9.0, first = 0.0, '
                   'interval = 1.0, name = "k", initial = "");\n'
                   'a = 1.0;\na;\n')
    assert not ok.problems, [p.message for p in ok.problems]


def test_the_verdict_tracks_the_topology_as_well_as_the_text():
    """A validation is invalidated by the structure moving, not just the text."""
    if os.environ.get("FUNNELFORGE_OFFICIAL") != "1":
        return
    if not (HAVE_REAL and REAL_CMS):
        return
    tmp = tempfile.mkdtemp(prefix="ff_topo_")
    cms = os.path.join(tmp, "copy.cms")
    shutil.copyfile(REAL_CMS, cms)
    doc = Document.open(REAL_POT)
    doc.run_local_layers()
    res = doc.validate_official(cms, timeout=300)
    assert res.ran and res.ok
    assert doc.official_is_current
    assert "desmond-valid" in doc.verdict().lower()

    with open(cms, "a") as fh:            # the structure changes underneath
        fh.write("\n")
    assert not doc.official_is_current
    assert "OFF009" in {b.code for b in doc.export_blockers()}
    v = doc.verdict().lower()
    assert "out of date" in v and "structure" in v
    assert not v.startswith("desmond-valid")
    shutil.rmtree(tmp, ignore_errors=True)


def test_official_validation_when_it_is_available():
    if os.environ.get("FUNNELFORGE_OFFICIAL") != "1":
        return
    if not (HAVE_REAL and os.path.exists(REAL_CMS)):
        return
    doc = Document.open(REAL_POT)
    doc.run_local_layers()
    res = doc.validate_official(REAL_CMS, timeout=300)
    assert res.ran and res.ok, res.error_message
    m = res.manifest
    for key in ("pot_sha256", "cms_sha256", "schrodinger_path", "version",
                "argv", "exit_code", "timestamp_utc", "duration_s"):
        assert key in m, key
    assert doc.official_is_current
    assert "desmond-valid" in doc.verdict().lower()
    assert not doc.export_blockers()
    doc.set_source(doc.source + "\n")
    assert not doc.official_is_current
    assert doc.export_blockers()


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
