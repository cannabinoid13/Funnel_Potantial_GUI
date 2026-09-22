"""First-guess CV groups for a structure that has no potential file yet.

The heuristics are deliberately simple and are always reported back to the
user, because the choice of ligand and of the two frame groups is a modelling
decision, not something to be hidden:

* **ligand** - the largest non-solvent, non-ion, non-polymer residue,
* **site**   - backbone atoms of the pocket residues nearest the ligand,
* **core**   - backbone atoms of residues deepest in the protein body, chosen
  so that the CORE -> SITE axis points out of the pocket towards solvent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import elements
from .funnel import Selection


@dataclass
class GroupSuggestion:
    lig: Selection
    site: Selection
    core: Selection
    label: str = "LIG"
    notes: list[str] = field(default_factory=list)


def candidate_ligands(st, min_heavy: int = 3) -> list:
    """Every plausible ligand in the system, largest first.

    Returns ``(residue, n_heavy, 1-based heavy atom indices)`` so the caller
    can offer a choice instead of guessing.
    """
    hetero = st.mask("hetero") & ~st.mask("ion") & (st.anum > 1)
    out = []
    for r in st.residues:
        idx = [i + 1 for i in range(r.first, r.last) if hetero[i]]
        if len(idx) >= min_heavy:
            out.append((r, len(idx), idx))
    out.sort(key=lambda x: -x[1])
    return out


def residue_heavy_atoms(st, index1: int) -> list:
    """1-based heavy atoms of the residue an atom belongs to."""
    r = st.residue_of_atom(int(index1) - 1)
    if r is None:
        return []
    return [i + 1 for i in range(r.first, r.last) if st.anum[i] > 1]


def suggest_groups(st, n_site: int = 4, n_core: int = 4,
                   contact: float = 5.0, lig_indices=None,
                   site_indices=None, core_indices=None) -> GroupSuggestion:
    """Guess the CV groups, filling only what is missing.

    Any of ``lig_indices`` / ``site_indices`` / ``core_indices`` (1-based)
    that is supplied is kept exactly as it is; only the empty ones are worked
    out.  Nothing is replaced behind the user's back, and every choice - kept
    or made - comes back in ``notes``.
    """
    notes: list[str] = []
    hetero = st.mask("hetero") & ~st.mask("ion") & (st.anum > 1)
    candidates = candidate_ligands(st, min_heavy=6)

    if lig_indices:
        lig_idx = sorted(int(i) for i in lig_indices)
        res = st.residue_of_atom(lig_idx[0] - 1)
        labels = sorted({st.residue_of_atom(i - 1).long_label
                         for i in lig_idx
                         if st.residue_of_atom(i - 1) is not None})
        notes.append(f"ligand: kept your selection - {len(lig_idx)} atoms in "
                     + ", ".join(labels))
    else:
        if not candidates:
            raise ValueError("No non-solvent hetero residue with at least 6 "
                             "heavy atoms was found; pick the ligand by hand.")
        n_lig, res, lig_idx = (candidates[0][1], candidates[0][0],
                               candidates[0][2])
        notes.append(f"ligand: {res.long_label} with {n_lig} heavy atoms "
                     f"(largest of {len(candidates)} hetero residues)")
        if len(candidates) > 1:
            others = ", ".join(f"{r.long_label} ({n})"
                               for r, n, _i in candidates[1:4])
            notes.append(f"other hetero residues left out: {others}")

    lig_com = st.center_of_mass(np.asarray(lig_idx) - 1)
    keep_site = sorted(int(i) for i in site_indices) if site_indices else None
    keep_core = sorted(int(i) for i in core_indices) if core_indices else None

    # --- pocket residues: protein residues in contact with the ligand
    prot = st.mask("protein") & (st.anum > 1)
    prot_idx = np.where(prot)[0]
    if prot_idx.size == 0:
        raise ValueError("No protein atoms found; the funnel frame needs a "
                         "rigid body.")
    lig_pts = st.xyz[np.asarray(lig_idx) - 1]
    try:
        from scipy.spatial import cKDTree
        tree = cKDTree(lig_pts)
        d, _ = tree.query(st.xyz[prot_idx], k=1)
    except ImportError:
        d = np.min(np.linalg.norm(
            st.xyz[prot_idx][:, None, :] - lig_pts[None, :, :], axis=2), axis=1)
    if keep_site is not None:
        site_idx = keep_site
        notes.append("site: kept your selection - "
                     + ", ".join(st.residue_signature(
                         np.asarray(site_idx) - 1)[:6]))
        site_com = st.center_of_mass(np.asarray(site_idx) - 1)
    near = prot_idx[d <= contact]
    pocket: list = []
    seen = set()
    for i in near:
        r = st.residue_of_atom(int(i))
        if r is None or r.long_label in seen:
            continue
        seen.add(r.long_label)
        ca = _backbone(st, r)
        if not ca:
            continue
        com = st.center_of_mass(np.asarray(ca) - 1)
        pocket.append((float(np.linalg.norm(com - lig_com)), r, ca))
    pocket.sort(key=lambda x: x[0])
    if keep_site is None:
        if len(pocket) < 2:
            raise ValueError("Fewer than two pocket residues with a full "
                             "backbone were found within %.1f A of the "
                             "ligand." % contact)
        chosen_site = pocket[:max(2, n_site)]
        site_idx = sorted(i for _, _, ca in chosen_site for i in ca)
        notes.append("site: " + ", ".join(r.label for _, r, _ in chosen_site)
                     + f" (backbone N/CA/C/O within {contact:.0f} A of the "
                       "ligand)")
        site_com = st.center_of_mass(np.asarray(site_idx) - 1)

    # --- body residues: buried, and behind the pocket along the exit axis
    body_com = st.center_of_mass(prot_idx)
    outward = site_com - body_com
    n_out = np.linalg.norm(outward)
    outward = (np.array([0.0, 0.0, 1.0]) if n_out < 1e-6 else outward / n_out)
    try:
        from scipy.spatial import cKDTree
        ptree = cKDTree(st.xyz[prot_idx])
        burial_all = np.array([len(x) for x in
                               ptree.query_ball_point(st.xyz[prot_idx], 8.0)])
    except ImportError:
        burial_all = np.full(prot_idx.size, 100)
    burial_of = dict(zip(prot_idx.tolist(), burial_all.tolist()))
    cand = []
    for r in st.residues:
        if r.resname.strip().upper() not in elements.AA3:
            continue
        bb = _backbone(st, r)
        if not bb:
            continue
        com = st.center_of_mass(np.asarray(bb) - 1)
        ca = bb[1] - 1
        cand.append((float((com - body_com) @ outward),
                     int(burial_of.get(ca, 0)), r, bb))
    if not cand:
        raise ValueError("No protein residue with a complete backbone was "
                         "found for the CORE group.")
    buried_cut = float(np.percentile([c[1] for c in cand], 60))
    buried = [c for c in cand if c[1] >= buried_cut] or cand
    # farthest behind the pocket, so the CORE -> SITE axis is long and stable
    buried.sort(key=lambda x: x[0])
    if keep_core is not None:
        core_idx = keep_core
        notes.append("core: kept your selection - "
                     + ", ".join(st.residue_signature(
                         np.asarray(core_idx) - 1)[:6]))
    else:
        chosen_core = buried[:max(2, n_core)]
        core_idx = sorted(i for _, _, _, bb in chosen_core for i in bb)
        notes.append("core: " + ", ".join(r.label
                                          for _, _, r, _ in chosen_core)
                     + " (buried backbone furthest behind the pocket along "
                       "the exit direction)")

    core_com = st.center_of_mass(np.asarray(core_idx) - 1)
    axis_len = float(np.linalg.norm(st.min_image(site_com - core_com)))
    notes.append(f"CORE -> SITE axis length {axis_len:.2f} A; funnel origin starts halfway along it")
    if axis_len < 8.0:
        notes.append("WARNING: that axis is short; widen the CORE group or "
                     "pick residues deeper in the body.")

    label = (res.resname.strip() if res is not None else "") or "LIG"
    return GroupSuggestion(
        lig=Selection("lig", lig_idx,
                      f"chain {res.chain} and resnum {res.resnum} and heavy"),
        site=Selection("site", site_idx),
        core=Selection("core", core_idx),
        label=label, notes=notes)


def suggest_frame_reference(st, spec, model, n_res: int = 2) -> list[int]:
    """Backbone atoms that fix a perpendicular direction for off-axis work.

    Wanted: buried (so it does not flap around), far from the axis (so the
    perpendicular direction is well conditioned), and rigid.  Returns 1-based
    atom indices, or [] when nothing suitable exists.
    """
    fr = model.frame
    prot = np.where(st.mask("protein") & (st.anum > 1))[0]
    if prot.size == 0:
        return []
    try:
        from scipy.spatial import cKDTree
        tree = cKDTree(st.xyz[prot])
        burial = np.array([len(x) for x in
                           tree.query_ball_point(st.xyz[prot], 8.0)])
    except ImportError:
        burial = np.full(prot.size, 100)
    burial_of = dict(zip(prot.tolist(), burial.tolist()))
    cut = float(np.percentile(burial, 55)) if burial.size else 0.0
    best = []
    for r in st.residues:
        if r.resname.strip().upper() not in elements.AA3:
            continue
        bb = _backbone(st, r)
        if not bb:
            continue
        ca = bb[1] - 1
        if burial_of.get(ca, 0) < cut:
            continue
        com = st.center_of_mass(np.asarray(bb) - 1)
        v = st.min_image(com - fr.core_com)
        perp = v - float(v @ fr.axis) * fr.axis
        d = float(np.linalg.norm(perp))
        along = abs(float(v @ fr.axis))
        # far from the axis, and not strung out along it
        best.append((d - 0.25 * along, d, r, bb))
    if not best:
        return []
    best.sort(key=lambda x: -x[0])
    chosen = best[:max(1, n_res)]
    if chosen[0][1] < 5.0:
        return []
    return sorted(i for _s, _d, _r, bb in chosen for i in bb)


def _backbone(st, res) -> list[int]:
    """1-based N/CA/C/O indices of a residue, or [] if incomplete."""
    want = {"N": None, "CA": None, "C": None, "O": None}
    for i in range(res.first, res.last):
        name = str(st.atomname[i])
        if name in want and want[name] is None:
            want[name] = i + 1
    if any(v is None for v in want.values()):
        return []
    return [want[k] for k in ("N", "CA", "C", "O")]
