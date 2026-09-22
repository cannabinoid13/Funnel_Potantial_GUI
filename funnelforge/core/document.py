"""The document model: the source text is the truth, everything else is a view.

A :class:`Document` owns one ``.pot`` source string.  Every structure derived
from it - tokens, CST, symbols, dependency graph, diagnostics, the official
engine's verdict - is tagged with the *revision* of the text it was computed
from, so a result can never be shown against text it does not describe.  When
the text changes, stale results say so rather than quietly lying.

Two ways out of a document, deliberately different:

``save_source``
    writes exactly what the user has, byte for byte, atomically, refusing to
    clobber a file that changed on disk underneath.  Always available: a file
    this interface cannot fully model is still a file the user may edit and
    keep.

``export_run_ready``
    the stricter path.  It refuses when the syntax layer failed, when anything
    opaque contributes to the potential, or when the official Desmond parser
    has not accepted *this* revision against *this* topology.  Nothing is
    written until those hold, so a run-ready export can never be the moment
    physics goes missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import safety
from .lang import cst, patch, sema
from .lang import diagnostics as diag
from .lang import registry as reg


def _resolve_asl(text: str, structure, asl_mod):
    """Resolve one ASL string, preferring the exact reader for each form.

    Maestro's ``atom. 1,2,3-9`` list form is what Desmond potentials
    overwhelmingly contain, and :func:`funnelforge.core.potfile.parse_asl_atoms`
    reads it exactly; the general selection language handles the rest.  When
    neither can, the caller reports "not understood *here*" - a statement
    about this interface, never about Desmond.
    """
    import re
    from .potfile import parse_asl_atoms
    stripped = text.strip()
    low = stripped.lower()
    # the Maestro spellings that all mean "these atom indices"
    m = re.match(r"(?:atom|a)\s*\.\s*(?:num|n)?\s*|atom\s+", low)
    if m and m.end() > 0:
        # parse_asl_atoms skips anything it cannot read, so `atom. 1,2,foo`
        # would come back as two atoms and look authoritative.  Check the
        # payload is nothing but integers and ranges before trusting it; if
        # it is not - `atom.ptype " CA "`, say - refuse, and the caller
        # reports it as a form this interface does not understand.
        payload = stripped[m.end():]
        pieces = [x for x in re.split(r"[,\s]+", payload.strip()) if x]
        if not pieces or not all(re.fullmatch(r"\d+(-\d+)?", x)
                                 for x in pieces):
            raise ValueError(
                "this is not a plain atom-index list; only "
                "`atom. <ids and lo-hi ranges>` is read here, the rest is "
                "left to Desmond")
        idx = parse_asl_atoms(stripped)
        if idx:
            return list(idx)
        raise ValueError("the atom list is empty")
    # The local picker returns zero-based array positions, while the plain
    # Maestro list and this document's topology table use one-based atom IDs.
    return [int(i) + 1 for i in asl_mod.parse_selection(stripped, structure)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: parser/sema problem code -> catalogue code, when the two differ
_CODE_ALIASES: dict = {}


@dataclass
class Revision:
    """One state of the source text."""

    text: str
    rev: str
    note: str = ""

    @staticmethod
    def of(text: str, note: str = "") -> "Revision":
        return Revision(text, safety.text_digest(text), note)


@dataclass
class Document:
    """A ``.pot`` source and the analyses derived from it."""

    source: str = ""
    path: str = ""
    encoding: "safety.Encoding | None" = None
    report: diag.Report = field(default_factory=diag.Report)
    release: str = reg.DEFAULT_RELEASE

    _parse: "cst.ParseResult | None" = field(default=None, repr=False)
    _analysis: "sema.Analysis | None" = field(default=None, repr=False)
    _analysed_rev: str = ""
    _parsed_rev: str = ""
    _undo: list = field(default_factory=list, repr=False)
    _redo: list = field(default_factory=list, repr=False)
    _saved_rev: str = ""
    _disk_digest: str = ""
    topology_path: str = ""
    topology_digest: str = ""
    selection_table: list = field(default_factory=list)
    _topology_key: int = 0
    _topology_obj: object = None
    last_official: object = None

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @staticmethod
    def open(path: str, limits: "safety.Limits" = safety.DEFAULT_LIMITS
             ) -> "Document":
        real = safety.canonical_path(path, must_exist=True, must_be_file=True)
        text, enc = safety.read_text_file(real, limits)
        doc = Document(source=text, path=real, encoding=enc)
        doc._saved_rev = doc.rev
        doc._disk_digest = safety.file_digest(real)
        return doc

    @staticmethod
    def from_text(text: str, path: str = "",
                  encoding: "safety.Encoding | None" = None) -> "Document":
        doc = Document(source=text, path=path,
                       encoding=encoding or safety.sniff_encoding(
                           text.encode("utf-8")))
        doc._saved_rev = doc.rev
        return doc

    # ------------------------------------------------------------------
    # revisions
    # ------------------------------------------------------------------
    @property
    def rev(self) -> str:
        return safety.text_digest(self.source)

    @property
    def modified(self) -> bool:
        return self.rev != self._saved_rev

    def set_source(self, text: str, note: str = "edit") -> None:
        """Replace the whole text - the raw-editor path."""
        if text == self.source:
            return
        self._undo.append(Revision.of(self.source, note))
        self._redo.clear()
        self.source = text
        self._invalidate()

    def apply_edits(self, edits, note: str = "structured edit"):
        """Apply span edits, proving that nothing outside them moved.

        Returns the :class:`funnelforge.core.lang.patch.PatchResult`.  The
        check is not decoration: it is the mechanical guarantee behind
        "a structured edit modifies only the expected source spans".
        """
        edits = list(edits)
        if not edits:
            return patch.apply(self.source, [])
        res = patch.apply(self.source, edits)
        if not patch.unchanged_outside(self.source, res.text, edits):
            raise RuntimeError(
                "the edit changed text outside its own spans; refusing")
        self._undo.append(Revision.of(self.source, note))
        self._redo.clear()
        self.source = res.text
        self._invalidate()
        return res

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(Revision.of(self.source, "redo"))
        self.source = self._undo.pop().text
        self._invalidate()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(Revision.of(self.source, "undo"))
        self.source = self._redo.pop().text
        self._invalidate()
        return True

    def _invalidate(self) -> None:
        self._parse = None
        self._analysis = None
        self._analysed_rev = ""
        self.report.stale(self.rev)

    # ------------------------------------------------------------------
    # derived views
    # ------------------------------------------------------------------
    def parsed(self, limits: "safety.Limits" = safety.DEFAULT_LIMITS
               ) -> "cst.ParseResult":
        if self._parse is None or self._parsed_rev != self.rev:
            self._parse = cst.parse(self.source,
                                    max_depth=limits.max_ast_depth,
                                    max_tokens=limits.max_tokens)
            self._parsed_rev = self.rev
        return self._parse

    def analysis(self, topology=None,
                 limits: "safety.Limits" = safety.DEFAULT_LIMITS
                 ) -> "sema.Analysis":
        """The semantic view of the current text.

        The cache is keyed on the revision *and* on the topology, because an
        analysis made without a structure has unresolved selections and must
        not be handed back once a structure has been supplied.  Passing no
        topology reuses whatever is cached rather than throwing away a
        resolved analysis.
        """
        want = id(topology) if topology is not None else self._topology_key
        if (self._analysis is None or self._analysed_rev != self.rev
                or (topology is not None and self._topology_key != want)):
            self._analysis = sema.analyse(
                self.parsed(limits), release=self.release,
                topology=topology if topology is not None
                else self._topology_obj,
                max_dependency_depth=limits.max_dependency_depth)
            self._analysed_rev = self.rev
            if topology is not None:
                self._topology_key = want
                self._topology_obj = topology
        return self._analysis

    # ------------------------------------------------------------------
    # the interface's own layers
    # ------------------------------------------------------------------
    def run_local_layers(self, topology=None) -> diag.Report:
        """Syntax, model coverage, symbols and lint, for the current text."""
        rev = self.rev
        res = self.parsed()
        an = self.analysis(topology)
        self.report.reset("syntax")
        self.report.reset("model")
        self.report.reset("symbols")
        self.report.reset("lint")

        for p in res.problems:
            self.report.add(self._to_diag(p))
        enc = self.encoding
        if enc is not None:
            if getattr(enc, "bom", False):
                self.report.add(diag.make(
                    "SYN013", "the file starts with a UTF-8 byte-order mark; "
                              "it is preserved on save", file=self.path))
            if getattr(enc, "newline", "") == "mixed":
                self.report.add(diag.make(
                    "SYN014", "the file mixes LF and CRLF line endings; they "
                              "are preserved exactly as they are",
                    file=self.path))
        self.report.mark(
            "syntax",
            diag.State.ERROR if any(
                d.severity == "error"
                for d in self.report.layers["syntax"].diagnostics)
            else diag.State.PASSED,
            note=f"{len(res.tokens)} tokens, {len(res.statements())} statements",
            source_rev=rev)

        # model coverage: opaque nodes are preserved, but say so plainly
        for node in res.opaque_nodes:
            line, col = self._pos(node.start)
            self.report.add(diag.make(
                "MOD001",
                "this construct is preserved exactly but is not modelled by "
                "the structured view",
                evidence=node.span_text(self.source)[:200],
                file=self.path, line=line, col=col,
                span=(node.start, node.end)))
        for name, nodes in an.unknown_functions.items():
            for node in nodes:
                line, col = self._pos(node.start)
                self.report.add(diag.make(
                    "MOD003",
                    f"`{name}()` is not in the {self.release} function "
                    "registry; it is preserved unchanged and left for the "
                    "official validator to judge",
                    evidence=node.span_text(self.source)[:200],
                    file=self.path, line=line, col=col,
                    span=(node.start, node.end)))
        lossy_nodes = list(an.opaque_reaching_energy)
        if an.export_is_lossy() and not lossy_nodes:
            lossy_nodes = [c.node for c in an.contributions if c.opaque] or \
                ([an.energy_stmt] if an.energy_stmt is not None else [])
        for node in lossy_nodes:
            line, col = self._pos(node.start)
            self.report.add(diag.make(
                "MOD004",
                "an unmodelled construct contributes to the potential, so a "
                "structured export could silently drop physics",
                evidence=node.span_text(self.source)[:200],
                file=self.path, line=line, col=col,
                span=(node.start, node.end)))
        self.report.mark("model", self._state("model"),
                         note=self._coverage_note(an), source_rev=rev)

        for p in an.problems:
            if p in res.problems:
                continue
            self.report.add(self._to_diag(p))
        self._metad_notes(an)
        self.report.mark("symbols", self._state("symbols"),
                         note=f"{len(an.symbols)} names, "
                              f"{len(an.reachable)} reach the potential",
                         source_rev=rev)

        self._lint(an)
        self.report.mark("lint", self._state("lint"), source_rev=rev)
        return self.report

    def _coverage_note(self, an) -> str:
        res = self.parsed()
        total = len(res.statements())
        opaque = len(res.opaque_nodes)
        known = total - opaque
        pct = 100.0 * known / total if total else 100.0
        return (f"{known}/{total} statements modelled ({pct:.0f}%), "
                f"{len(an.unknown_functions)} unrecognised function(s)")

    def _metad_notes(self, an) -> None:
        if not an.metas:
            self.report.add(diag.make(
                "MTD001", "no meta() call was found, so this file applies no "
                          "metadynamics bias", file=self.path))
            return
        for mc in an.metas:
            line, col = self._pos(mc.node.start)
            if mc.role == sema.PROBE:
                self.report.add(diag.make(
                    "MTD009",
                    f"zero-height meta({mc.index}, ...) reads the accumulated "
                    "bias back; this is a probe, not a deposit",
                    file=self.path, line=line, col=col,
                    span=(mc.node.start, mc.node.end)))
            if mc.wt is not None and mc.wt.confirmed:
                extra = ""
                if mc.wt.ktemp is not None:
                    extra += f" kDT = {mc.wt.ktemp:.6g} kcal/mol"
                if mc.wt.h0 is not None:
                    extra += f", h0 = {mc.wt.h0:.6g} kcal/mol"
                self.report.add(diag.make(
                    "MTD007",
                    "well-tempered relation confirmed from the data flow for "
                    f"accumulator {mc.index}.{extra}",
                    evidence=mc.wt.evidence, file=self.path, line=line,
                    col=col, span=(mc.node.start, mc.node.end)))
            elif mc.wt is not None and mc.role != sema.PROBE:
                self.report.add(diag.make(
                    "MTD008",
                    f"MetaD detected on accumulator {mc.index}; well-tempered "
                    "construction not confirmed",
                    evidence=mc.wt.evidence, file=self.path, line=line,
                    col=col, span=(mc.node.start, mc.node.end)))

    def _lint(self, an) -> None:
        """Physical and numerical sanity; findings, never rewrites."""
        for sym in an.symbols.values():
            v = sema.const_value(an, sym.value)
            if v is None:
                continue
            if v != v or v in (float("inf"), float("-inf")):
                line, col = self._pos(sym.bind.start)
                self.report.add(diag.make(
                    "LNT006", f"`{sym.name}` folds to a non-finite value",
                    evidence=sym.bind.span_text(self.source)[:120],
                    file=self.path, line=line, col=col,
                    span=(sym.bind.start, sym.bind.end)))
        for mc in an.metas:
            items = sema.resolve_array(an, mc.hills) if mc.hills else None
            if not items or len(items) < 2:
                continue
            for w in items[1:]:
                wv = sema.const_value(an, w)
                if wv is not None and wv <= 0 and not mc.zero_height:
                    line, col = self._pos(w.start)
                    self.report.add(diag.make(
                        "LNT007",
                        f"a hill width of {wv:g} is not usable; widths must "
                        "be positive",
                        file=self.path, line=line, col=col,
                        span=(w.start, w.end)))

    def _state(self, layer: str) -> "diag.State":
        ds = self.report.layers[layer].diagnostics
        if any(d.severity == "error" for d in ds):
            return diag.State.ERROR
        if any(d.severity == "warning" for d in ds):
            return diag.State.WARNING
        return diag.State.PASSED

    def _to_diag(self, p) -> "diag.Diagnostic":
        line, col = self._pos(p.start)
        end_line, end_col = self._pos(p.end)
        code = _CODE_ALIASES.get(p.code, p.code)
        if code not in diag.CATALOG:
            # never invent a verdict for a code we do not know: fall back to
            # the most conservative generic in the right layer
            code = "SYN001" if code.startswith("SYN") else "SEM001"
        return diag.make(code, p.message, evidence=p.evidence,
                         file=self.path, line=line, col=col,
                         end_line=end_line, end_col=end_col,
                         span=(p.start, p.end))

    def _pos(self, offset: int) -> tuple:
        from .lang.lexer import position
        return position(self.source, offset)

    # ------------------------------------------------------------------
    # the topology layer
    # ------------------------------------------------------------------
    def resolve_topology(self, cms_path: str | None = None, structure=None):
        """Resolve every atom selection against a real structure.

        Selections are never reordered or rewritten - only counted and
        reported.  A selection this interface's ASL reader cannot parse is
        recorded as *not understood here*, which is a statement about the
        interface, not about Desmond: the official layer decides that.
        """
        rev = self.rev
        self.report.reset("topology")
        st = structure
        if st is None:
            path = cms_path or self.topology_path
            if not path:
                self.report.add(diag.make(
                    "TOP004", "no structure was supplied, so atom selections "
                              "are unresolved", file=self.path))
                self.report.mark("topology", diag.State.NOT_RUN,
                                 note="no .cms supplied", source_rev=rev)
                self.selection_table = []
                return []
            from .cms import Structure
            st = Structure.load(safety.canonical_path(
                path, must_exist=True, must_be_file=True))
            self.topology_path = safety.canonical_path(path)
            self.topology_digest = safety.file_digest(self.topology_path)

        from . import asl as asl_mod
        an = self.analysis(topology=st)
        table: list = []
        for sym in an.selections:
            call = next((n for n in sym.value.walk()
                         if n.kind == cst.CALL and n.name == "atomsel"), None)
            text = ""
            if call is not None and call.children and \
                    call.children[0].kind == cst.STR:
                text = call.children[0].text.strip('"')
            line, col = self._pos(sym.bind.start)
            row = {"name": sym.name, "asl": text, "line": line,
                   "n": None, "chains": [], "residues": [], "note": ""}
            try:
                idx = _resolve_asl(text, st, asl_mod)
            except Exception as exc:
                row["note"] = str(exc)
                self.report.add(diag.make(
                    "TOP008",
                    f"`{sym.name}`: this interface's selection reader could "
                    f"not evaluate {text!r}; it is preserved unchanged and "
                    "left to Desmond",
                    evidence=str(exc), file=self.path, line=line, col=col,
                    span=(sym.bind.start, sym.bind.end)))
                table.append(row)
                continue
            idx = list(idx)
            row["n"] = len(idx)
            if not idx:
                self.report.add(diag.make(
                    "TOP001", f"`{sym.name}` selects no atoms in this "
                              f"structure", evidence=text, file=self.path,
                    line=line, col=col, span=(sym.bind.start, sym.bind.end)))
            else:
                bad = [i for i in idx if i < 1 or i > st.n_atoms]
                if bad:
                    self.report.add(diag.make(
                        "TOP002",
                        f"`{sym.name}` names atom {bad[0]}, outside "
                        f"1..{st.n_atoms}", evidence=text, file=self.path,
                        line=line, col=col))
                    row["note"] = (f"{len(bad)} atom id(s) outside "
                                   f"1..{st.n_atoms}")
                    table.append(row)
                    continue
                res = [st.residue_of_atom(int(i) - 1) for i in idx]
                row["chains"] = sorted({getattr(r, "chain", "") for r in res})
                row["residues"] = sorted({getattr(r, "long_label", str(r))
                                          for r in res})[:12]
                import numpy as np
                sel = np.asarray(idx, dtype=np.int64) - 1
                if st.mask("water")[sel].any() or st.mask("ion")[sel].any():
                    self.report.add(diag.make(
                        "TOP003",
                        f"`{sym.name}` includes water or ions; that is "
                        "sometimes deliberate, so nothing has been changed",
                        evidence=text, file=self.path, line=line, col=col))
            table.append(row)
        self.selection_table = table
        self.report.mark("topology", self._state("topology"),
                         note=f"{len(table)} selection(s) against "
                              f"{os.path.basename(self.topology_path)}",
                         source_rev=rev)
        return table

    # ------------------------------------------------------------------
    # the official layer
    # ------------------------------------------------------------------
    def validate_official(self, cms_path: str | None = None, *,
                          installation=None, timeout: float = 180.0,
                          cancel=None):
        """Ask the installed Desmond parser, and record it against this rev."""
        from .desmond import adapter
        rev = self.rev
        cms = cms_path or self.topology_path or None
        result = adapter.validate(self.source, cms, installation=installation,
                                  timeout=timeout, cancel=cancel)
        self.report.reset("official")
        for d in result.diagnostics:
            d.file = d.file or self.path
            self.report.add(d)
        if not result.ran:
            state = diag.State.NOT_RUN
        elif result.ok:
            state = diag.State.PASSED
        else:
            state = diag.State.ERROR
        self.report.mark("official", state,
                         note=self._official_note(result), source_rev=rev)
        self.last_official = result
        if cms:
            self.topology_path = cms
            self.topology_digest = safety.file_digest(cms)
        return result

    def _official_note(self, result) -> str:
        m = result.manifest
        if not result.ran:
            return result.error_message or "not run"
        return (f"{m.get('version', '?')} build {m.get('build', '?')}, "
                f"exit {m.get('exit_code')}, {result.duration_s:.2f} s, "
                f"pot {m.get('pot_sha256', '')[:12]}")

    @property
    def official_is_current(self) -> bool:
        st = self.report.layers["official"]
        if st.state is not diag.State.PASSED:
            return False
        if st.source_rev != self.rev:
            return False
        if self.topology_path:
            return safety.file_digest(self.topology_path) == \
                self.topology_digest
        return True

    # ------------------------------------------------------------------
    # the package layer
    # ------------------------------------------------------------------
    def check_package(self, msj_path: str = "", cfg_path: str = "",
                      sh_path: str = "", cms_path: str = ""):
        """Cross-file checks over the job this potential belongs to.

        Only decidable things are reported.  Where a claim would need a guess
        - which temperature a hand-written potential intends, say - the value
        is surfaced rather than judged, because inventing a conflict is worse
        than reporting none.
        """
        rev = self.rev
        self.report.reset("package")
        base = os.path.splitext(self.path)[0] if self.path else ""
        jobname = os.path.basename(base)

        def sibling(ext: str, given: str) -> str:
            if given:
                return safety.canonical_path(given)
            cand = base + ext
            return cand if base and os.path.exists(cand) else ""

        msj_path = sibling(".msj", msj_path)
        cfg_path = sibling(".cfg", cfg_path)
        sh_path = sibling(".sh", sh_path)
        cms_path = sibling(".cms", cms_path or self.topology_path)
        found = {k: v for k, v in (("msj", msj_path), ("cfg", cfg_path),
                                   ("sh", sh_path), ("cms", cms_path)) if v}
        from .blocktext import BlockText
        texts: dict = {}
        for kind, path in found.items():
            if kind == "cms":
                continue
            try:
                texts[kind] = open(path, encoding="utf-8",
                                   errors="replace").read()
            except OSError as exc:
                self.report.add(diag.make(
                    "PKG010", f"the {kind} file cannot be read: {exc}",
                    file=path))

        pot_name = os.path.basename(self.path) if self.path else ""

        # -- the .msj production stage must switch the bias on and name us
        if "msj" in texts:
            mb = BlockText(texts["msj"])
            stages = [c for c in mb.root.children if c.key == "simulate"]
            enabled = False
            for st in stages:
                node = st.child("meta")
                if node is not None and mb.raw(node).strip().strip('"') \
                        == "FILE":
                    enabled = True
                    mf = st.child("meta_file")
                    named = mb.raw(mf).strip().strip('"') if mf else ""
                    if named and pot_name and \
                            os.path.basename(named) != pot_name:
                        self.report.add(diag.make(
                            "PKG002",
                            f"the .msj production stage loads {named!r}, not "
                            f"{pot_name!r}", evidence=named, file=msj_path))
                    if named:
                        ref = os.path.join(os.path.dirname(msj_path),
                                           os.path.basename(named))
                        if not os.path.exists(ref) and \
                                "$" not in named:
                            self.report.add(diag.make(
                                "PKG001",
                                f"the .msj names {named!r}, which is not next "
                                "to it", evidence=named, file=msj_path))
            if stages and not enabled:
                self.report.add(diag.make(
                    "PKG008",
                    "no simulate stage in the .msj sets meta = FILE, so the "
                    "production run would carry no bias at all",
                    file=msj_path))

        # -- the .cfg must leave meta_file for Multisim and agree on names
        if "cfg" in texts:
            cb = BlockText(texts["cfg"])
            mf = cb.value("meta_file")
            if mf is not None and mf.strip() not in ("?", ""):
                named = mf.strip().strip('"')
                if pot_name and os.path.basename(named) != pot_name:
                    self.report.add(diag.make(
                        "PKG002",
                        f"the .cfg pins meta_file to {named!r}, not "
                        f"{pot_name!r}", evidence=named, file=cfg_path))

        # -- the output names the potential declares
        an = self.analysis()
        for d in an.declarations:
            name = d.output_name
            # a name holding $JOBNAME is filled in by Multisim, so there is
            # nothing to compare - but that must not skip the checks below it
            stem = "" if (not name or "$" in name) \
                else os.path.basename(name).split(".")[0]
            if jobname and stem and stem != jobname:
                self.report.add(diag.make(
                    "PKG003",
                    f"{d.which} writes {name!r}, which does not follow the "
                    f"job name {jobname!r}", evidence=name, file=self.path,
                    line=self._pos(d.node.start)[0]))
            initial = d.terms.get("initial")
            if initial is not None and initial.kind == cst.STR:
                ker = initial.text.strip('"')
                if ker and "$" not in ker:
                    ref = os.path.join(os.path.dirname(self.path) or ".",
                                       os.path.basename(ker))
                    if not os.path.exists(ref):
                        self.report.add(diag.make(
                            "PKG009",
                            f"{d.which} restarts from {ker!r}, which is not "
                            "next to the potential", evidence=ker,
                            file=self.path))

        # -- the launcher's job name
        if "sh" in texts and jobname:
            if jobname not in texts["sh"]:
                self.report.add(diag.make(
                    "PKG003",
                    f"the .sh does not mention the job name {jobname!r}",
                    file=sh_path))

        # -- anything that moved since the engine last looked
        if self.topology_path and self.topology_digest and \
                safety.file_digest(self.topology_path) != \
                self.topology_digest:
            self.report.add(diag.make(
                "PKG005", "the structure changed since it was resolved",
                file=self.topology_path))

        if found:
            note = ", ".join(f"{k}: {os.path.basename(v)}"
                             for k, v in found.items())
            state = self._state("package")
        else:
            # nothing to cross-check against, but the potential's own
            # declarations were still read; saying PASSED here would imply a
            # package check that never happened
            note = ("no .msj, .cfg, .sh or .cms alongside this potential; "
                    "only its own declarations were checked")
            got = self.report.layers["package"].diagnostics
            state = self._state("package") if got else diag.State.NOT_RUN
        self.report.mark("package", state, note=note, source_rev=rev)
        return self.report.layers["package"]

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def save_source(self, path: str | None = None, *,
                    allow_overwrite_changed: bool = False) -> str:
        """Write the text exactly as it stands, atomically."""
        target = safety.canonical_path(path or self.path)
        if not target:
            raise ValueError("no path to save to")
        expect = None
        if not allow_overwrite_changed and target == self.path \
                and self._disk_digest:
            expect = self._disk_digest
        data = self.encoding.encode(self.source) if self.encoding \
            else self.source.encode("utf-8")
        safety.atomic_write(target, data, expect_digest=expect)
        self.path = target
        self._saved_rev = self.rev
        self._disk_digest = safety.file_digest(target)
        return target

    def export_blockers(self) -> list:
        """Why a run-ready export is refused, or [] when it is allowed."""
        out: list = []
        out.extend(self.report.blocks_export())
        an = self.analysis()
        if an.export_is_lossy():
            bad = an.opaque_reaching_energy
            node = bad[0] if bad else (
                an.energy_stmt or self.parsed().program)
            line, col = self._pos(node.start)
            out.append(diag.make(
                "MOD004",
                "a construct the interface cannot model contributes to the "
                "potential; a structured export could drop it, so it is "
                "refused. Saving the source is still available and lossless.",
                evidence=node.span_text(self.source)[:200],
                file=self.path, line=line, col=col))
        st = self.report.layers["official"]
        if st.state is diag.State.NOT_RUN:
            out.append(diag.make(
                "OFF001",
                "the installed Desmond parser has not been run on this text, "
                "so nothing here may be called Desmond-valid",
                file=self.path))
        elif st.state is diag.State.STALE or st.source_rev != self.rev:
            out.append(diag.make(
                "OFF008",
                "the text changed after the last official validation; run it "
                "again before exporting", file=self.path))
        elif st.state is not diag.State.PASSED:
            out.append(diag.make(
                "OFF003", "the installed Desmond parser rejected this text",
                file=self.path))
        elif self.topology_path and \
                safety.file_digest(self.topology_path) != \
                self.topology_digest:
            out.append(diag.make(
                "OFF009",
                "the topology changed after the last official validation; "
                "run it again before exporting", file=self.path))
        seen: set = set()
        uniq: list = []
        for d in out:
            key = (d.code, d.message)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(d)
        return uniq

    def export_run_ready(self, path: str, *, force: bool = False) -> tuple:
        """Write the potential only when every gate is satisfied.

        Returns ``(written_path, blockers)``; ``written_path`` is "" when
        nothing was written, and nothing is written before the checks pass -
        the file on disk is never touched by a refused export.
        """
        blockers = self.export_blockers()
        if blockers and not force:
            return "", blockers
        target = safety.canonical_path(path)
        # The engine consumes UTF-8 text and rejects a leading U+FEFF. Source
        # saving preserves an input BOM/codec, but runnable output must use
        # the encoding accepted by the validator. Keep the newline convention.
        text = self.source[1:] if self.source.startswith("\ufeff") else self.source
        encoding = safety.Encoding("utf-8", False,
                                   self.encoding.newline if self.encoding else "lf",
                                   self.encoding.final_newline if self.encoding else True)
        data = encoding.encode(text)
        safety.atomic_write(target, data)
        return target, blockers

    # ------------------------------------------------------------------
    def verdict(self) -> str:
        """The honest headline, including the topology the engine ran against.

        ``Report.verdict()`` only knows about the *text* revision.  A
        validation is equally invalidated by the structure changing
        underneath it, so that case is corrected here rather than letting the
        headline claim more than :meth:`official_is_current` would allow.
        """
        line = self.report.verdict()
        st = self.report.layers["official"]
        if st.state is diag.State.PASSED and not self.official_is_current:
            head, _, tail = line.partition(". ")
            return ("Desmond validation is out of date - it ran against a "
                    "different structure. " + tail).strip()
        return line

    def external_change(self) -> bool:
        """Has the file changed on disk since it was read or last written?"""
        if not self.path or not os.path.exists(self.path):
            return False
        return safety.file_digest(self.path) != self._disk_digest
