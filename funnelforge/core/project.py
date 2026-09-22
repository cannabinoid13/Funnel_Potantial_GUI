"""The job: one .cms plus the .pot/.msj/.cfg/.sh that drive it.

``Job`` is the single object the interface edits.  It owns the structure, the
funnel spec, the three job files, and the export/verification pipeline that
writes a ready-to-run set into one directory.
"""

from __future__ import annotations

import json
import os
import time
import copy
import re
import shutil
import tempfile
from dataclasses import dataclass, field

import numpy as np

from .cms import Structure
from .funnel import FunnelModel, FunnelSpec, Issue, Selection, Diagnostic
from .potfile import parse_pot, emit_pot, ParseReport
from .jobfiles import (CfgFile, MsjFile, ShFile, BundlePaths, discover_bundle,
                       place_cms, CMS_COPY, bundle_replacements,
                       rewrite_file_references, stage_bundle_export,
                       finalize_bundle_export, validate_export_tree)
from .templates import default_msj, default_cfg
from .mexpr import evaluate_pot, MExprError

APP_NAME = "FunnelForge"
APP_VERSION = "1.0"


@dataclass
class ImportReport:
    loaded: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    pot_report: ParseReport | None = None
    messages: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class ExportResult:
    written: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    verified: bool = False
    verification: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(i.level == "error" for i in self.issues)


class Job:
    def __init__(self):
        self.structure: Structure | None = None
        self.spec: FunnelSpec = FunnelSpec()
        self.model: FunnelModel | None = None
        self.cfg: CfgFile | None = None
        self.msj: MsjFile | None = None
        self.sh: ShFile | None = None
        self.paths = BundlePaths()
        self.jobname: str = "funnel_metad_run"
        self.dirty: bool = False

    # ------------------------------------------------------------------
    # import
    # ------------------------------------------------------------------
    def import_bundle(self, any_path: str, progress=None) -> ImportReport:
        # Load into a fresh job so a failed import cannot leave a new
        # potential attached to the previous job's structure or launcher.
        candidate = Job()
        report = candidate._import_bundle(any_path, progress)
        self.__dict__.update(candidate.__dict__)
        return report

    def _import_bundle(self, any_path: str, progress=None) -> ImportReport:
        t0 = time.time()
        rep = ImportReport()
        # A new import starts from nothing: no funnel, no shapes, no leftover
        # geometry from the job that was open before.
        self.spec = FunnelSpec()
        self.model = None
        self.cfg = self.msj = None
        paths = discover_bundle(any_path)
        if not paths.any():
            raise FileNotFoundError(f"No job files found next to {any_path}")
        self.paths = paths
        stem = ""
        for p in (paths.cms, paths.pot, paths.msj, paths.cfg, paths.sh):
            if p:
                stem = os.path.splitext(os.path.basename(p))[0]
                break
        self.jobname = stem or self.jobname

        if paths.cms:
            if progress:
                progress(0.05, f"Reading {os.path.basename(paths.cms)}")
            self.structure = Structure.load(
                paths.cms, progress=(lambda f, m: progress(0.05 + 0.6 * f, m))
                if progress else None)
            rep.loaded.append(paths.cms)
        else:
            rep.missing.append(".cms structure")

        if paths.pot:
            if progress:
                progress(0.70, "Parsing potential")
            with open(paths.pot, encoding="utf-8", newline="") as fh:
                text = fh.read()
            self.spec, pot_rep = parse_pot(text, paths.pot)
            rep.pot_report = pot_rep
            rep.loaded.append(paths.pot)
        else:
            rep.missing.append(".pot potential")
            self.spec = FunnelSpec()

        if paths.cfg:
            self.cfg = CfgFile.load(paths.cfg)
            rep.loaded.append(paths.cfg)
            T = self.cfg.temperature()
            if T and self.spec.source_path:
                if abs(T - self.spec.temperature) > 1e-6:
                    rep.messages.append(
                        f"Potential comment says {self.spec.temperature:g} K "
                        f"while the .cfg runs at {T:g} K; using the .cfg value "
                        "for the well-tempered factor.")
                    self.spec.temperature = T
                    self.spec.gamma = self.spec.gamma_from_ktemp()
        else:
            rep.missing.append(".cfg configuration")

        if paths.msj:
            self.msj = MsjFile.load(paths.msj)
            rep.loaded.append(paths.msj)
        else:
            rep.missing.append(".msj stage chain")

        if paths.sh:
            self.sh = ShFile.load(paths.sh)
            rep.loaded.append(paths.sh)
            if self.sh.jobname:
                self.jobname = self.sh.jobname
        else:
            self.sh = ShFile()
            self.sh.jobname = self.jobname
            self.sh.description = "Funnel WT-MetaD"
            rep.missing.append(".sh launcher")

        if self.structure is not None:
            self._resolve_source_groups()
            self.model = FunnelModel(self.structure, self.spec)
            # Validate the recovered frame before the GUI attempts to render
            # it, so malformed inputs produce an import report, not a Qt
            # callback exception after the old scene has been replaced.
            if self.spec.source_adapter:
                self.model.frame
        rep.messages.extend(getattr(paths, "messages", []))
        if progress:
            progress(1.0, "Ready")
        rep.seconds = time.time() - t0
        self.dirty = False
        return rep

    def attach_structure(self, path: str, progress=None) -> None:
        self.structure = Structure.load(path, progress=progress)
        self.paths.cms = path
        self._resolve_source_groups()
        self.model = FunnelModel(self.structure, self.spec)

    def _resolve_source_groups(self):
        if not self.spec.source_adapter or self.structure is None:
            return
        from .document import _resolve_asl
        from . import asl
        metadata = self.spec.source_adapter
        for role in metadata["groups"]:
            selection = getattr(self.spec, role)
            if not selection.indices and selection.expression:
                selection.indices = _resolve_asl(selection.expression, self.structure, asl)
                # Resolving a selection is not an edit: retain the original
                # ASL expression until the user actually changes the group.
                metadata["group_baseline"][role] = list(selection.indices)

    # ------------------------------------------------------------------
    # derived quantities
    # ------------------------------------------------------------------
    @property
    def production_ps(self) -> float | None:
        if self.msj is not None:
            p = self.msj.production
            if p is not None and p.time_ps:
                return p.time_ps
        if self.cfg is not None:
            return self.cfg.number("time")
        return None

    @property
    def cutoff_radius(self) -> float:
        if self.cfg is not None:
            v = self.cfg.number("cutoff_radius")
            if v:
                return float(v)
        return 9.0

    def clamp_handle_value(self, key: str, value: float,
                           margin: float = 0.25) -> float:
        """Limit a dragged value so the shape stays inside the solvent box.

        Distances along a shape are measured on that shape's own axis, so a
        tilted or offset body is bounded correctly rather than by the funnel
        axis it no longer follows.
        """
        m = self.model
        sp = self.spec
        if m is None:
            return value
        import numpy as np
        try:
            if key.startswith("t:"):
                _p, var, what = key.split(":", 2)
                t = next((x for x in sp.terms if x.var == var), None)
                if t is None:
                    return value
                if what.startswith("tilt"):
                    return value            # bounded by clamp_tilts instead
                if what in ("e1", "e2"):
                    # the whole body has to fit, not just its origin
                    radius = (t.radius if t.kind == "sphere"
                              else (0.0 if t.kind == "slab"
                                    else m.term_profile_max(t, margin)))
                    lo, hi = m.lateral_limits(1 if what == "e1" else 2,
                                              radius=radius, margin=margin)
                    if hi <= lo:
                        lo, hi = m.lateral_limits(1 if what == "e1" else 2,
                                                  margin=margin)
                    return float(np.clip(value, lo, hi))
                if what == "R":
                    return float(np.clip(value, 0.1,
                                         max(0.1, m.term_max_radius(t,
                                                                    margin))))
                o, u, _v1, _v2 = m.term_frame(t)
                radius = (t.radius if t.kind == "sphere"
                          else (0.0 if t.kind == "slab"
                                else m.term_profile_max(t, margin)))
                radius = min(radius, m.best_room(margin))
                lo, hi = m.box_limits_along(o, u, radius=radius,
                                            margin=margin)
                if hi <= lo:
                    lo, hi = m.box_limits_along(o, u, margin=margin)
                return float(np.clip(value, lo, hi))
            if key.startswith("axis_tilt"):
                return value                # bounded by clamp_tilts instead
            if key in ("origin_lat1", "origin_lat2"):
                lo, hi = m.lateral_limits(1 if key.endswith("1") else 2,
                                          margin=margin)
                return float(np.clip(value, lo, hi))
            if key == "r_cyl":
                return float(np.clip(value, 0.1,
                                     max(0.1, m.best_room(margin))))
            if key == "cone":
                base_room = m.max_radius_at(np.array([sp.z_min]), margin)
                return float(np.clip(value, sp.r_cyl, max(sp.r_cyl,
                                                          base_room)))
            if key in ("z_min", "z_max", "z_cc", "origin"):
                radius = sp.r_cyl if key == "z_max" else min(
                    sp.r_base(), m.best_room(margin))
                lo, hi = m.axis_limits(radius=max(0.0, radius), margin=margin)
                if hi <= lo:
                    lo, hi = m.axis_limits(margin=margin)
                if key == "origin":
                    lo2, hi2 = m.axis_limits(margin=margin)
                    return float(np.clip(value, lo2, hi2))
                return float(np.clip(value, lo, hi))
        except Exception:
            return value
        return value

    def refresh_model(self) -> None:
        if self.structure is None:
            return
        if self.model is None:
            self.model = FunnelModel(self.structure, self.spec)
        else:
            self.model.spec = self.spec
            self.model.invalidate()

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def validate(self) -> list[Issue]:
        out: list[Issue] = []
        T = self.cfg.temperature() if self.cfg else None
        if self.model is not None:
            out += self.model.validate(cutoff_radius=self.cutoff_radius,
                                       production_ps=self.production_ps,
                                       cfg_temperature=T)
        else:
            out.append(Issue("error", "structure", "No .cms structure loaded",
                             "Atom indices and centres of mass cannot be "
                             "checked without the system."))
        pot_name = self.jobname + ".pot"
        cfg_name = self.jobname + ".cfg"
        if self.cfg is not None:
            box_min = float(np.linalg.norm(self.structure.box, axis=1).min()) \
                if self.structure is not None else None
            out += self.cfg.validate(self.spec, box_min)
        if self.msj is not None:
            out += self.msj.validate(pot_name, cfg_name, self.spec,
                                     self.cfg.number("time") if self.cfg else None)
        if self.sh is not None:
            out += self.sh.validate(self.jobname)
        return out

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------
    def pot_text(self) -> str:
        return emit_pot(self.spec, self.structure)

    def verify_pot_text(self, text: str) -> dict:
        """Re-read the emitted potential and compare it with the GUI model."""
        info: dict = {"ok": False}
        if self.structure is None or self.model is None:
            info["error"] = "no structure loaded"
            return info
        if self.spec.source_adapter:
            return self._verify_source_potential(text)
        try:
            return self._verify_designer_potential(text)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _verify_designer_potential(self, text: str) -> dict:
        info: dict = {"ok": False}
        try:
            res = evaluate_pot(text, self.structure)
        except MExprError as exc:
            info["error"] = str(exc)
            return info
        z_m, rho_m = self.model.cv_of_ligand()
        sp = self.spec
        exp = {
            "z": z_m,
            "rho": rho_m,
            "axis_len": self.model.frame.axis_len,
            "hill": sp.h0,
            "v_total": float(self.model.total_at_ligand()),
        }
        if sp.funnel_enabled:
            exp["r_allowed"] = float(sp.r_allowed(z_m))
            exp["v_rad"] = float(sp.v_rad(z_m, rho_m))
            exp["v_z"] = float(sp.v_zwall(z_m))
        for t in sp.terms:
            if not t.enabled:
                continue
            ctx = self.model.field_ctx(points=self.model.frame.lig_com[None, :])
            exp[t.energy_var] = float(np.asarray(t.energy(ctx)).reshape(-1)[0])
        got = {k: res.scalar(k) for k in exp}
        if not all(np.isfinite(v) for v in (*exp.values(), *got.values())):
            raise ValueError("A required preview value is missing or non-finite.")
        worst = 0.0
        worst_rel = 0.0
        rows = []
        for k in exp:
            d = abs(got[k] - exp[k])
            worst = max(worst, d)
            worst_rel = max(worst_rel, d / max(1.0, abs(exp[k])))
            rows.append((k, exp[k], got[k], d))
        # random probe cross-check over the funnel neighbourhood
        rng = np.random.default_rng(12345)
        fr = self.model.frame
        zs = rng.uniform(sp.z_min - 8, sp.z_max + 8, 4000)
        rr = rng.uniform(0, sp.r_base() + 8, 4000)
        ph = rng.uniform(0, 2 * np.pi, 4000)
        pts = fr.cyl_to_world(zs, rr, ph)
        res2 = evaluate_pot(text, self.structure, probe=pts)
        v_model = np.asarray(self.model.total_field(points=pts))
        v_file = res2.array("v_total")
        if v_file is None or not np.isfinite(v_model).all() or not np.isfinite(v_file).all():
            raise ValueError("The potential is missing or non-finite at probe positions.")
        diff = np.abs(v_model - v_file)
        field_err = float(np.nanmax(diff))
        # A steep wall reaches 1e9 kcal/mol a few angstrom out, where one ULP
        # of a double is already 1e-7.  Comparing two evaluation orders can
        # only ever agree to a *relative* precision, so that is what is
        # required; an absolute limit would flag pure rounding as an error.
        field_rel = float(np.nanmax(diff / np.maximum(1.0, np.abs(v_model))))
        tol = 1e-9
        expr_terms = [t.label or t.var for t in sp.terms
                      if t.enabled and t.kind in ("expression", "lathe")]
        info.update(ok=(worst_rel < tol and field_rel < tol),
                    rows=rows, worst=worst, worst_rel=worst_rel,
                    field_error=field_err, field_error_rel=field_rel,
                    tolerance=tol,
                    declares=res.declares, prints=res.printed(),
                    n_probe=len(pts), expression_terms=expr_terms)
        return info

    def _verify_source_potential(self, text: str) -> dict:
        """Check the actual imported expressions, including the final energy.

        The preview uses the spec's source patches; the independent input
        here is the exact text about to be written. No fixed terminal energy
        variable or a particular funnel frame template is required.
        """
        info = {"ok": False}
        try:
            sp, model = self.spec, self.model
            parsed = evaluate_pot(text, self.structure, probe_group=sp.lig.var)
            actual = np.asarray(parsed.value.data, dtype=float)
            if actual.size != 1 or not np.isfinite(actual).all():
                raise ValueError("The potential must return one finite energy value.")
            expected = model.total_at_ligand()
            rows = [("potential", expected, float(actual.reshape(-1)[0]),
                     abs(expected - float(actual.reshape(-1)[0])))]
            variables = sp.source_adapter["variables"]
            for name, target_cv in zip(("z", "rho"), model.cv_of_ligand()):
                actual_cv = parsed.scalar(variables[name])
                if not np.isfinite(actual_cv) or not np.isfinite(target_cv):
                    raise ValueError(f"The {name} coordinate is missing or non-finite.")
                rows.append((name, target_cv, actual_cv, abs(target_cv - actual_cv)))
            # Spatial probing catches edits that leave the starting energy
            # unchanged, such as a wall's location or force constant.
            rng = np.random.default_rng(12345)
            lo, hi, radius = model.field_bounds(pad=5.0)
            zz = rng.uniform(lo, hi, 4000)
            rr = rng.uniform(0.0, radius, 4000)
            ph = rng.uniform(0.0, 2 * np.pi, 4000)
            points = model.frame.cyl_to_world(zz, rr, ph)
            observed = evaluate_pot(text, self.structure, probe=points,
                                    probe_group=sp.lig.var)
            field = np.asarray(observed.value.data, dtype=float)
            if field.shape[-1:] != (1,):
                raise ValueError("The final potential is not scalar at probe positions.")
            field = np.broadcast_to(field[..., 0], (len(points),))
            target = np.broadcast_to(np.asarray(model.total_field(points=points)),
                                     (len(points),))
            if not np.isfinite(field).all() or not np.isfinite(target).all():
                raise ValueError("Non-finite energy at one or more probe positions.")
            diff = np.abs(field - target)
            relative = diff / np.maximum(1.0, np.abs(target))
            scalar_rel = max(row[3] / max(1.0, abs(row[1])) for row in rows)
            worst_rel = max(scalar_rel, float(relative.max()))
            info.update(ok=worst_rel < 1e-9, rows=rows, worst=max(row[3] for row in rows),
                        worst_rel=scalar_rel, field_error=float(diff.max()),
                        field_error_rel=float(relative.max()), tolerance=1e-9,
                        n_probe=len(points), declares=parsed.declares,
                        prints=parsed.printed(), expression_terms=["Imported source"])
        except Exception as exc:
            info["error"] = str(exc)
        return info

    def export(self, outdir: str, jobname: str | None = None,
               cms_mode: str = CMS_COPY,
               write: tuple[bool, bool, bool, bool] = (True, True, True, True),
               sync: bool = True, force: bool = False) -> ExportResult:
        """Verify and stage a complete export before replacing its directory.

        Source files are never overwritten. Explicit partial exports remain
        available, with missing dependencies reported as warnings. ``force``
        may retain an unverified potential, but cannot bypass file failures.
        """
        res = ExportResult()
        jobname = jobname or self.jobname
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", jobname or ""):
            res.issues.append(Issue("error", "export", "Invalid job name",
                                    "Use a filename made of letters, digits, underscores, hyphens and dots."))
            return res
        if len(write) != 4 or cms_mode not in ("copy", "hardlink", "symlink", "skip"):
            res.issues.append(Issue("error", "export", "Invalid export options", ""))
            return res
        if cms_mode == "symlink" and self.paths.archive_path:
            cms_mode = "copy"
            res.issues.append(Issue("info", "export", "Copied the CMS from the imported ZIP",
                                    "A symbolic link to temporary archive extraction would break when the application exits."))
        outdir = os.path.abspath(os.path.expanduser(outdir))
        inputs = [getattr(self.paths, attr) for attr in ("cms", "pot", "msj", "cfg", "sh")]
        if os.path.islink(outdir) or any(
                p and os.path.commonpath([os.path.realpath(p), os.path.realpath(outdir)]) == os.path.realpath(outdir)
                for p in inputs):
            res.issues.append(Issue("error", "export", "Choose an export directory separate from the imported inputs",
                                    "The source package is preserved for reproducibility."))
            return res
        previous = self.cfg, self.msj, self.sh, self.jobname
        previous_outputs = self.spec.cvseq_name, self.spec.kerseq_name
        self.cfg, self.msj, self.sh = (copy.deepcopy(f) for f in previous[:3])
        staging = backup = ""
        committed = False
        try:
            created = self.ensure_job_files(jobname) if sync else []
            if sync:
                self.sync_files(jobname)
            for what in created:
                res.issues.append(Issue("info", "export", f"Created a {what}",
                                        "A standard Desmond protocol supplies the missing job file."))
            text = self.pot_text()
            if text.startswith('\ufeff'):
                text = text[1:]
                res.issues.append(Issue("info", "encoding", "Removed the leading byte-order mark for Desmond",
                                        "The imported source is preserved; the runnable potential uses plain UTF-8."))
            res.verification = self.verify_pot_text(text)
            res.verified = bool(res.verification.get("ok"))
            if not res.verified:
                res.issues.append(Issue("error", "verify", "The generated potential failed numerical verification",
                                        str(res.verification.get("error", "The emitted field disagrees with the GUI model."))))
                if not force:
                    return res
            problems = [i for i in self.validate() if i.level in ("error", "warning")]
            res.issues.extend(problems)
            if any(i.level == "error" for i in problems):
                return res
            complete = all(write) and cms_mode != "skip"
            if not complete:
                res.issues.append(Issue("warning", "export", "Partial job export selected",
                                        "Omitted inputs must already be available before this job can run."))
            os.makedirs(os.path.dirname(outdir), exist_ok=True)
            staging = tempfile.mkdtemp(prefix=".funnelforge-export-", dir=os.path.dirname(outdir))
            replacements = bundle_replacements(self.paths, jobname) if sync else {}
            staged = stage_bundle_export(self.paths, staging, replacements)
            contents = (text, self.msj.text if self.msj else None,
                        self.cfg.text if self.cfg else None,
                        self.sh.render() if self.sh else None)
            for selected, ext, content in zip(write, ("pot", "msj", "cfg", "sh"), contents):
                if not selected:
                    continue
                if content is None:
                    raise ValueError(f"No .{ext} file is available for the selected export.")
                target = os.path.join(staging, f"{jobname}.{ext}")
                if os.path.lexists(target):
                    raise FileExistsError(f"A package dependency collides with {jobname}.{ext}")
                _write(target, content)
                if ext == "sh":
                    os.chmod(target, 0o755)
                staged.append(target)
            if self.paths.cms and cms_mode != "skip":
                target = os.path.join(staging, f"{jobname}.cms")
                placed = place_cms(self.paths.cms, target, cms_mode)
                if not placed or not os.path.isfile(placed):
                    raise OSError("The CMS structure was not placed in the exported package.")
                staged.append(placed)
            elif complete:
                raise ValueError("No CMS structure is available for this job.")
            # Existing unselected files are retained, including inputs the
            # explicit partial-export controls told us not to rewrite.
            if os.path.exists(outdir):
                if not os.path.isdir(outdir):
                    raise NotADirectoryError(outdir)
                for root, dirs, names in os.walk(outdir, followlinks=False):
                    relative = os.path.relpath(root, outdir)
                    target_root = staging if relative == "." else os.path.join(staging, relative)
                    os.makedirs(target_root, exist_ok=True)
                    for name in names:
                        target = os.path.join(target_root, name)
                        if not os.path.lexists(target):
                            shutil.copy2(os.path.join(root, name), target, follow_symlinks=False)
                    for name in list(dirs):
                        source = os.path.join(root, name)
                        if os.path.islink(source):
                            target = os.path.join(target_root, name)
                            if not os.path.lexists(target):
                                os.symlink(os.readlink(source), target)
                            dirs.remove(name)
            geometry = {}
            if self.model is not None:
                frame = self.model.frame
                geometry = {"axis": frame.axis.tolist(), "origin": frame.origin.tolist(),
                            **{key: float(getattr(self.spec, key)) for key in
                               ("z_min", "z_max", "z_cc", "r_cyl", "cone_slope", "k_rad", "k_z")}}
            staged += finalize_bundle_export(self.paths, staging, replacements,
                                              res.verification, jobname, geometry)
            dependencies = validate_export_tree(staging, jobname, complete=complete)
            res.issues.extend(dependencies)
            if any(i.level == "error" for i in dependencies):
                return res
            # The whole prepared directory is committed, with rollback if the
            # final rename fails. No half-written runnable job is exposed.
            relative_written = sorted({os.path.relpath(p, staging) for p in staged})
            if os.path.exists(outdir):
                backup = tempfile.mkdtemp(prefix=".funnelforge-previous-", dir=os.path.dirname(outdir))
                os.rmdir(backup)
                os.replace(outdir, backup)
            try:
                os.replace(staging, outdir)
            except BaseException:
                if backup:
                    os.replace(backup, outdir)
                    backup = ""
                raise
            staging = ""
            committed = True
            res.written = [os.path.join(outdir, p) for p in relative_written]
        except Exception as exc:
            res.issues.append(Issue("error", "export", "Export was not written", str(exc)))
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
            if backup:
                shutil.rmtree(backup, ignore_errors=True)
            if not committed:
                self.cfg, self.msj, self.sh, self.jobname = previous
                self.spec.cvseq_name, self.spec.kerseq_name = previous_outputs
        return res

    def ensure_job_files(self, jobname: str) -> list[str]:
        """Create a standard .msj/.cfg/.sh for a job that arrived without one.

        A structure imported on its own cannot be run by a potential alone;
        rather than exporting an incomplete set, the ordinary Desmond
        relaxation chain and backend configuration are written, wired to this
        potential.  Everything stays editable in the Job files tab.
        """
        made: list[str] = []
        T = self.spec.temperature or 310.0
        ps = self.production_ps or 500000.0
        if self.cfg is None:
            self.cfg = CfgFile(default_cfg(temperature=T, production_ps=ps),
                               "")
            made.append(".cfg (standard Desmond production settings)")
        if self.msj is None:
            self.msj = MsjFile(default_msj(jobname, temperature=T,
                                           production_ps=ps), "")
            made.append(".msj (standard relaxation chain + FILE metadynamics)")
        if self.sh is None:
            self.sh = ShFile()
            self.sh.jobname = jobname
            self.sh.description = "Funnel WT-MetaD"
            made.append(".sh (multisim launcher)")
        return made

    def sync_files(self, jobname: str) -> list[str]:
        """Make input references agree while preserving custom launch logic."""
        changed: list[str] = []
        replacements = bundle_replacements(self.paths, jobname)
        for ext in ("pot", "cfg", "msj", "cms", "sh"):
            replacements[f"{self.jobname}.{ext}"] = f"{jobname}.{ext}"
        old_stems = {self.jobname}
        for path in (self.paths.pot, self.paths.sh):
            if path:
                old_stems.add(os.path.splitext(os.path.basename(path))[0])
        for attr, suffix in (("cvseq_name", ".cvseq"), ("kerseq_name", ".kerseq")):
            value = getattr(self.spec, attr)
            if value in {stem + suffix for stem in old_stems}:
                setattr(self.spec, attr, jobname + suffix)
                if value != jobname + suffix:
                    changed.append("pot." + attr)
        if self.msj is not None:
            p = self.msj.production
            if p is not None and p.meta_file != f"{jobname}.pot":
                self.msj.set_meta_file(f"{jobname}.pot")
                changed.append("msj.meta_file")
            p = self.msj.production
            if p is not None and p.cfg_file != f"{jobname}.cfg":
                self.msj.set_cfg_file(f"{jobname}.cfg")
                changed.append("msj.cfg_file")
        if self.cfg is not None:
            source = self.cfg.text
            updated = rewrite_file_references(source, replacements)
            if updated != source:
                self.cfg = CfgFile(updated, self.cfg.path)
                changed.append("cfg.references")
            T = self.cfg.temperature()
            if T is not None and abs(T - self.spec.temperature) > 1e-9:
                self.cfg.set_temperature(self.spec.temperature)
                changed.append("cfg.temperature")
        if self.sh is not None:
            self.sh.rewrite_references(replacements)
            if self.sh.jobname != jobname:
                self.sh.jobname = jobname
                changed.append("sh.JOBNAME")
        self.jobname = jobname
        return changed

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def save_session(self, path: str) -> None:
        data = {
            "app": APP_NAME, "version": APP_VERSION,
            "jobname": self.jobname,
            "paths": self.paths.__dict__,
            "spec": self.spec.to_dict(),
            "job_files": {
                "cfg": self.cfg.text if self.cfg else None,
                "msj": self.msj.text if self.msj else None,
                "sh": self.sh.render() if self.sh else None,
            },
        }
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)

    def load_session(self, path: str, progress=None) -> ImportReport:
        with open(path) as fh:
            data = json.load(fh)
        paths = data.get("paths", {})
        candidates = [paths.get(k) for k in ("cms", "pot", "cfg", "archive_path")]
        anchor = next((p for p in candidates if p and os.path.exists(p)), None)
        if anchor is None:
            raise FileNotFoundError("The session's input files and source ZIP are unavailable.")
        rep = self.import_bundle(anchor, progress=progress)
        spec_d = data.get("spec", {})
        self.apply_spec_dict(spec_d)
        for attr, cls in (("cfg", CfgFile), ("msj", MsjFile), ("sh", ShFile)):
            if attr in data.get("job_files", {}):
                text = data["job_files"][attr]
                setattr(self, attr, cls(text, getattr(self.paths, attr) or "")
                        if text is not None else None)
        self.jobname = data.get("jobname", self.jobname)
        return rep

    def apply_spec_dict(self, d: dict) -> None:
        sp = FunnelSpec()
        for k, v in d.items():
            if k in ("lig", "site", "core", "frame_ref"):
                setattr(sp, k, Selection(v.get("var", k), list(v.get("indices", [])),
                                         v.get("expression", "")))
            elif k == "diagnostics":
                sp.diagnostics = [Diagnostic(x.get("var", "d"), x.get("label", ""),
                                             list(x.get("indices", [])),
                                             x.get("dist_var", ""),
                                             x.get("expression", ""))
                                  for x in v]
            elif k == "prints":
                sp.prints = [tuple(x) for x in v]
            elif k == "terms":
                from .terms import Term
                sp.terms = [Term.from_dict(x) for x in v]
            elif hasattr(sp, k):
                setattr(sp, k, v)
        self.spec = sp
        self.refresh_model()


def _write(path: str, text: str) -> None:
    with open(path, "w", newline="\n") as fh:
        fh.write(text)
