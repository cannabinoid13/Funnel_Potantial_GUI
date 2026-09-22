"""Structure model for a Desmond ``.cms`` system.

The model keeps everything the interface needs to draw the system and to
reproduce, atom-for-atom, the centre-of-mass arithmetic that the Desmond
backend performs while evaluating a ``.pot`` M-expression:

* global 1-based atom indices exactly as ``atomsel("atom. N")`` sees them
  (the full_system ct is the concatenation of the component cts),
* force-field masses read from ``ffio_sites`` when available,
* the chorus box vectors of the supplied solvent box,
* bonds, residues, chains and per-ct provenance (solute / ion / solvent).
"""

from __future__ import annotations

import os
import pickle
import hashlib
from dataclasses import dataclass

import numpy as np

from . import elements
from .maestro import MaestroFile, CtBlock

CACHE_VERSION = 4


@dataclass
class Residue:
    key: tuple[str, int, str, str]      # chain, resnum, inscode, resname
    chain: str
    resnum: int
    inscode: str
    resname: str
    first: int                          # 0-based atom slice
    last: int                           # exclusive
    ct: int

    @property
    def natoms(self) -> int:
        return self.last - self.first

    @property
    def label(self) -> str:
        ins = self.inscode.strip()
        return f"{self.resname.strip()}{self.resnum}{ins}"

    @property
    def long_label(self) -> str:
        return f"{self.chain}:{self.label}"


@dataclass
class CtSummary:
    index: int
    title: str
    ct_type: str
    natoms: int
    offset: int                          # 0-based offset in the global order


class Structure:
    """Geometry + topology of a solvated Desmond system."""

    def __init__(self):
        self.path: str = ""
        self.title: str = ""
        self.n_atoms: int = 0
        self.xyz: np.ndarray = np.zeros((0, 3), dtype=np.float64)
        self.anum: np.ndarray = np.zeros(0, dtype=np.int16)
        self.mass: np.ndarray = np.zeros(0, dtype=np.float64)
        self.resnum: np.ndarray = np.zeros(0, dtype=np.int32)
        self.chain: np.ndarray = np.zeros(0, dtype="<U6")
        self.resname: np.ndarray = np.zeros(0, dtype="<U6")
        self.atomname: np.ndarray = np.zeros(0, dtype="<U6")
        self.inscode: np.ndarray = np.zeros(0, dtype="<U2")
        self.ss: np.ndarray = np.zeros(0, dtype=np.int8)
        self.bfactor: np.ndarray = np.zeros(0, dtype=np.float32)
        self.charge: np.ndarray = np.zeros(0, dtype=np.float32)
        self.ct_index: np.ndarray = np.zeros(0, dtype=np.int16)
        self.bonds: np.ndarray = np.zeros((0, 2), dtype=np.int32)
        self.bond_order: np.ndarray = np.zeros(0, dtype=np.int8)
        self.box: np.ndarray = np.eye(3) * 100.0
        self.cts: list[CtSummary] = []
        self.residues: list[Residue] = []
        self._res_of_atom: np.ndarray = np.zeros(0, dtype=np.int32)
        self._res_lookup: dict[tuple[str, int, str], int] = {}
        # cached masks
        self._masks: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str, use_cache: bool = True, progress=None) -> "Structure":
        path = os.path.abspath(path)
        cache = _cache_path(path)
        if use_cache and cache and os.path.exists(cache):
            try:
                with open(cache, "rb") as fh:
                    obj = pickle.load(fh)
                if isinstance(obj, dict) and obj.get("_v") == CACHE_VERSION:
                    st = cls()
                    st.__dict__.update(obj["data"])
                    if progress:
                        progress(1.0, "cache")
                    return st
            except Exception:
                pass
        st = cls()
        st._read(path, progress=progress)
        if use_cache and cache:
            try:
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                with open(cache, "wb") as fh:
                    pickle.dump({"_v": CACHE_VERSION, "data": st.__dict__}, fh,
                                protocol=pickle.HIGHEST_PROTOCOL)
            except Exception:
                pass
        return st

    def _read(self, path: str, progress=None) -> None:
        if progress:
            progress(0.05, "reading file")
        mf = MaestroFile(path)
        self.path = path
        full = mf.find_ct("full_system") or (mf.cts[0] if mf.cts else None)
        if full is None:
            raise ValueError("No f_m_ct block found in %s" % path)
        self.title = full.title
        self.box = _box_from_ct(full)

        atoms = full.arrays.get("m_atom")
        if atoms is None:
            raise ValueError("full_system ct has no m_atom table")
        self.n_atoms = atoms.nrows
        if progress:
            progress(0.15, "atoms")
        self._read_atoms(mf, atoms)
        if progress:
            progress(0.70, "bonds")
        bonds = full.arrays.get("m_bond")
        if bonds is not None:
            self._read_bonds(mf, bonds)
        if progress:
            progress(0.85, "component cts")
        self._read_components(mf, full)
        if progress:
            progress(0.95, "residues")
        self._build_residues()
        self._masks.clear()
        if progress:
            progress(1.0, "done")

    def _read_atoms(self, mf: MaestroFile, blk) -> None:
        n = blk.nrows
        ci = blk.column_index
        c_x = ci("r_m_x_coord")
        c_y = ci("r_m_y_coord")
        c_z = ci("r_m_z_coord")
        c_res = ci("i_m_residue_number")
        c_chain = ci("s_m_chain_name")
        c_rname = ci("s_m_pdb_residue_name")
        c_aname = ci("s_m_pdb_atom_name", "s_m_atom_name")
        c_anum = ci("i_m_atomic_number")
        c_ss = ci("i_m_secondary_structure")
        c_ins = ci("s_m_insertion_code")
        c_bf = ci("r_m_pdb_tfactor")
        c_q = ci("r_m_charge1")
        needed = [c for c in (c_x, c_y, c_z, c_res, c_chain, c_rname, c_aname,
                              c_anum, c_ss, c_ins, c_bf, c_q) if c]
        limit = max(needed) + 1 if needed else None
        if c_x is None or c_y is None or c_z is None:
            raise ValueError("m_atom table lacks coordinates")

        xyz = np.zeros((n, 3), dtype=np.float64)
        anum = np.zeros(n, dtype=np.int16)
        resnum = np.zeros(n, dtype=np.int32)
        ss = np.zeros(n, dtype=np.int8)
        bfac = np.zeros(n, dtype=np.float32)
        chg = np.zeros(n, dtype=np.float32)
        chain = np.empty(n, dtype="<U6")
        resname = np.empty(n, dtype="<U6")
        atomname = np.empty(n, dtype="<U6")
        inscode = np.empty(n, dtype="<U2")

        i = 0
        for toks in mf.rows(blk, limit=limit):
            if i >= n:
                break
            xyz[i, 0] = float(toks[c_x])
            xyz[i, 1] = float(toks[c_y])
            xyz[i, 2] = float(toks[c_z])
            if c_anum:
                t = toks[c_anum]
                anum[i] = int(t) if t not in ("<>", "") else 0
            if c_res:
                t = toks[c_res]
                resnum[i] = int(t) if t not in ("<>", "") else 0
            if c_chain:
                t = toks[c_chain]
                chain[i] = "" if t == "<>" else t.strip()
            if c_rname:
                t = toks[c_rname]
                resname[i] = "" if t == "<>" else t.strip()
            if c_aname:
                t = toks[c_aname]
                atomname[i] = "" if t == "<>" else t.strip()
            if c_ins:
                t = toks[c_ins]
                inscode[i] = "" if t == "<>" else t.strip()
            if c_ss:
                t = toks[c_ss]
                ss[i] = int(t) if t not in ("<>", "") else 0
            if c_bf:
                t = toks[c_bf]
                if t not in ("<>", ""):
                    try:
                        bfac[i] = float(t)
                    except ValueError:
                        pass
            if c_q:
                t = toks[c_q]
                if t not in ("<>", ""):
                    try:
                        chg[i] = float(t)
                    except ValueError:
                        pass
            i += 1
        self.xyz, self.anum, self.resnum = xyz, anum, resnum
        self.chain, self.resname, self.atomname = chain, resname, atomname
        self.inscode, self.ss, self.bfactor, self.charge = inscode, ss, bfac, chg
        self.ct_index = np.zeros(n, dtype=np.int16)
        # provisional masses from the periodic table; refined from ffio below
        self.mass = np.array([elements.mass(int(z)) for z in anum], dtype=np.float64)

    def _read_bonds(self, mf: MaestroFile, blk) -> None:
        c_from = blk.column_index("i_m_from")
        c_to = blk.column_index("i_m_to")
        c_ord = blk.column_index("i_m_order")
        if not c_from or not c_to:
            return
        limit = max(c_from, c_to, c_ord or 0) + 1
        pairs = np.zeros((blk.nrows, 2), dtype=np.int32)
        orders = np.ones(blk.nrows, dtype=np.int8)
        i = 0
        for toks in mf.rows(blk, limit=limit):
            if i >= blk.nrows:
                break
            try:
                a = int(toks[c_from]) - 1
                b = int(toks[c_to]) - 1
            except (ValueError, IndexError):
                continue
            pairs[i, 0] = a
            pairs[i, 1] = b
            if c_ord:
                t = toks[c_ord]
                if t not in ("<>", ""):
                    try:
                        orders[i] = int(float(t))
                    except ValueError:
                        pass
            i += 1
        pairs = pairs[:i]
        orders = orders[:i]
        ok = (pairs[:, 0] >= 0) & (pairs[:, 1] >= 0) & \
             (pairs[:, 0] < self.n_atoms) & (pairs[:, 1] < self.n_atoms)
        self.bonds = pairs[ok]
        self.bond_order = orders[ok]

    def _read_components(self, mf: MaestroFile, full: CtBlock) -> None:
        """Record component cts and pull force-field masses from ffio_sites."""
        offset = 0
        for ct in mf.cts:
            if ct is full:
                continue
            atoms = ct.arrays.get("m_atom")
            natoms = atoms.nrows if atoms is not None else 0
            if natoms == 0:
                continue
            self.cts.append(CtSummary(index=ct.index, title=ct.title,
                                      ct_type=ct.ct_type, natoms=natoms,
                                      offset=offset))
            if offset + natoms <= self.n_atoms:
                self.ct_index[offset:offset + natoms] = len(self.cts) - 1
                self._apply_ffio_masses(mf, ct, offset, natoms)
            offset += natoms
        if not self.cts:                        # single-ct file
            self.cts.append(CtSummary(index=full.index, title=full.title,
                                      ct_type=full.ct_type or "full_system",
                                      natoms=self.n_atoms, offset=0))
        if offset != self.n_atoms:
            # Component concatenation does not line up; fall back to one ct.
            self.ct_index[:] = 0

    def _apply_ffio_masses(self, mf: MaestroFile, ct: CtBlock,
                           offset: int, natoms: int) -> None:
        sites = ct.arrays.get("ffio_sites")
        if sites is None:
            return
        c_type = sites.column_index("s_ffio_type")
        c_mass = sites.column_index("r_ffio_mass")
        if not c_mass:
            return
        limit = max(c_type or 0, c_mass) + 1
        masses: list[float] = []
        for toks in mf.rows(sites, limit=limit):
            if c_type and len(toks) > c_type:
                if toks[c_type].strip().lower() != "atom":
                    continue
            try:
                masses.append(float(toks[c_mass]))
            except (ValueError, IndexError):
                return
        if not masses:
            return
        m = np.asarray(masses, dtype=np.float64)
        if m.size == natoms:
            self.mass[offset:offset + natoms] = m
        elif natoms % m.size == 0:
            self.mass[offset:offset + natoms] = np.tile(m, natoms // m.size)

    def _build_residues(self) -> None:
        n = self.n_atoms
        self.residues = []
        self._res_of_atom = np.zeros(n, dtype=np.int32)
        if n == 0:
            return
        chain, resnum, resname = self.chain, self.resnum, self.resname
        ins, ctidx = self.inscode, self.ct_index
        start = 0
        for i in range(1, n + 1):
            new = (i == n) or (resnum[i] != resnum[start] or
                               chain[i] != chain[start] or
                               resname[i] != resname[start] or
                               ins[i] != ins[start] or
                               ctidx[i] != ctidx[start])
            if new:
                r = Residue(key=(str(chain[start]), int(resnum[start]),
                                 str(ins[start]), str(resname[start])),
                            chain=str(chain[start]), resnum=int(resnum[start]),
                            inscode=str(ins[start]), resname=str(resname[start]),
                            first=start, last=i, ct=int(ctidx[start]))
                self._res_of_atom[start:i] = len(self.residues)
                self.residues.append(r)
                start = i
        self._res_lookup = {}
        for k, r in enumerate(self.residues):
            self._res_lookup.setdefault((r.chain, r.resnum, r.inscode), k)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    @property
    def box_lengths(self) -> np.ndarray:
        return np.linalg.norm(self.box, axis=1)

    @property
    def box_center(self) -> np.ndarray:
        """Where the periodic cell sits.

        Desmond centres the cell on the origin; if a file clearly does not
        (all-positive coordinates, say) fall back to the midpoint of the atom
        bounding box so the cell is still drawn around the system.
        """
        if self.n_atoms == 0:
            return np.zeros(3)
        lo = self.xyz.min(axis=0)
        hi = self.xyz.max(axis=0)
        mid = 0.5 * (lo + hi)
        L = np.linalg.norm(self.box, axis=1)
        return np.zeros(3) if np.all(np.abs(mid) < 0.25 * L) else mid

    @property
    def is_orthorhombic(self) -> bool:
        b = self.box
        off = b - np.diag(np.diag(b))
        return bool(np.all(np.abs(off) < 1e-6))

    def residue_of_atom(self, index0: int) -> Residue | None:
        if 0 <= index0 < self.n_atoms:
            return self.residues[int(self._res_of_atom[index0])]
        return None

    def find_residue(self, chain: str, resnum: int, inscode: str = "") -> Residue | None:
        k = self._res_lookup.get((chain, int(resnum), inscode))
        return self.residues[k] if k is not None else None

    def mask(self, name: str) -> np.ndarray:
        """Cached boolean masks for the common structural classes."""
        m = self._masks.get(name)
        if m is not None:
            return m
        rn_up = np.char.upper(self.resname)
        if name == "water":
            m = np.isin(rn_up, list(elements.WATER_NAMES))
            if not m.any():
                ct_water = {c.index for c in self.cts
                            if c.ct_type.lower() in ("solvent", "water")}
                if ct_water:
                    m = np.isin(self.ct_index, [i for i, c in enumerate(self.cts)
                                                if c.ct_type.lower() in ("solvent", "water")])
        elif name == "ion":
            single = np.zeros(self.n_atoms, dtype=bool)
            for r in self.residues:
                if r.natoms == 1 and str(r.resname).upper() not in elements.WATER_NAMES:
                    single[r.first:r.last] = True
            m = single & np.isin(rn_up, list(elements.ION_NAMES)) | \
                (single & (self.anum > 2) & ~np.isin(rn_up, list(elements.AA3)))
        elif name == "protein":
            m = np.isin(rn_up, list(elements.AA3.keys()))
        elif name == "nucleic":
            m = np.isin(rn_up, list(elements.NUCLEIC))
        elif name == "hydrogen":
            m = self.anum == 1
        elif name == "heavy":
            m = self.anum > 1
        elif name == "backbone":
            m = self.mask("protein") & np.isin(self.atomname,
                                               list(elements.BACKBONE_HEAVY))
        elif name == "ca":
            m = self.mask("protein") & (self.atomname == "CA")
        elif name == "hetero":
            m = ~(self.mask("protein") | self.mask("nucleic") |
                  self.mask("water") | self.mask("ion"))
        elif name == "solute":
            m = ~(self.mask("water"))
        else:
            m = np.zeros(self.n_atoms, dtype=bool)
        self._masks[name] = m
        return m

    def center_of_mass(self, idx0: np.ndarray) -> np.ndarray:
        """Mass-weighted centre of the given 0-based atom indices."""
        idx0 = np.asarray(idx0, dtype=np.int64)
        if idx0.size == 0:
            return np.zeros(3)
        w = self.mass[idx0]
        tot = w.sum()
        if tot <= 0:
            return self.xyz[idx0].mean(axis=0)
        return (self.xyz[idx0] * w[:, None]).sum(axis=0) / tot

    def min_image(self, v: np.ndarray) -> np.ndarray:
        """Desmond ``min_image``: wrap a displacement into the periodic cell."""
        v = np.asarray(v, dtype=np.float64)
        if self.is_orthorhombic:
            L = np.diag(self.box).astype(float)
            out = v.copy()
            for k in range(3):
                if L[k] > 0:
                    out[..., k] -= L[k] * np.round(out[..., k] / L[k])
            return out
        # General triclinic: reduce the fractional coordinates.  This is the
        # same reduced-coordinate wrap Desmond applies, and is exact for cells
        # that are not strongly skewed.
        H = self.box.T
        Hinv = np.linalg.inv(H)
        f = v @ Hinv.T
        f -= np.round(f)
        return f @ H.T

    def chains(self) -> list[str]:
        seen: list[str] = []
        for c in self.chain:
            s = str(c)
            if s not in seen:
                seen.append(s)
        return seen

    def describe_atom(self, index1: int) -> str:
        i = index1 - 1
        if not (0 <= i < self.n_atoms):
            return f"atom {index1}: out of range"
        r = self.residue_of_atom(i)
        el = elements.symbol(int(self.anum[i]))
        return (f"{index1}  {el:<2s} {self.atomname[i]:<4s} "
                f"{r.long_label if r else '?'}")

    def residue_signature(self, idx0: np.ndarray) -> list[str]:
        """Ordered unique residue labels (Leu110 style) for a selection."""
        out: list[str] = []
        for i in np.asarray(idx0, dtype=np.int64):
            r = self.residue_of_atom(int(i))
            if r is None:
                continue
            rn = r.resname.strip().capitalize()
            lbl = f"{rn}{r.resnum}{r.inscode.strip()}"
            if lbl not in out:
                out.append(lbl)
        return out


def _box_from_ct(ct: CtBlock) -> np.ndarray:
    keys = (("r_chorus_box_ax", "r_chorus_box_ay", "r_chorus_box_az"),
            ("r_chorus_box_bx", "r_chorus_box_by", "r_chorus_box_bz"),
            ("r_chorus_box_cx", "r_chorus_box_cy", "r_chorus_box_cz"))
    box = np.zeros((3, 3))
    for i, row in enumerate(keys):
        for j, k in enumerate(row):
            box[i, j] = ct.prop_float(k, 0.0)
    if np.allclose(box, 0.0):
        box = np.eye(3) * 100.0
    return box


def _cache_path(path: str) -> str | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = hashlib.sha1(
        f"{path}|{stat.st_size}|{int(stat.st_mtime)}|{CACHE_VERSION}".encode()
    ).hexdigest()[:20]
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "funnelforge", f"{key}.pkl")
