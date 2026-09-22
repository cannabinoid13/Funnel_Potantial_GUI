#!/usr/bin/env python3
"""Post-process the requested AChE contact panel from a Desmond trajectory.

The analysis is deliberately performed on saved DTR frames rather than in the
enhanced-sampling potential. It therefore adds no force-evaluation overhead to
the production simulation. All atom IDs below are tied to the supplied CMS.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
from itertools import chain
import json
import math
from pathlib import Path
import tempfile
from typing import Iterable

import numpy as np
from schrodinger.application.desmond.packages import analysis, topo, traj


SCRIPT_VERSION = "2.0"
DEFAULT_CONTACT_CUTOFF_A = 4.0

LIGAND_AIDS = list(range(8286, 8320))
LIGAND_IDENTITY = ("B", 1, "LIG1")

# These are exactly the side-chain heavy atoms in the supplied CMS.
RESIDUE_SPECS = {
    "Asp74": {
        "identity": ("A", 74, "ASP"),
        "aids": list(range(560, 564)),
        "atom_names": ("CB", "CG", "OD1", "OD2"),
    },
    "Trp86": {
        "identity": ("A", 86, "TRP"),
        "aids": list(range(654, 664)),
        "atom_names": (
            "CB", "CG", "CD1", "CD2", "NE1", "CE2", "CE3", "CZ2",
            "CZ3", "CH2",
        ),
    },
    "Tyr124": {
        "identity": ("A", 124, "TYR"),
        "aids": list(range(969, 977)),
        "atom_names": ("CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ", "OH"),
    },
    "Trp286": {
        "identity": ("A", 286, "TRP"),
        "aids": list(range(2160, 2170)),
        "atom_names": (
            "CB", "CG", "CD1", "CD2", "NE1", "CE2", "CE3", "CZ2",
            "CZ3", "CH2",
        ),
    },
    "Phe297": {
        "identity": ("A", 297, "PHE"),
        "aids": list(range(2259, 2266)),
        "atom_names": ("CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ"),
    },
    "Tyr337": {
        "identity": ("A", 337, "TYR"),
        "aids": list(range(2555, 2563)),
        "atom_names": ("CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ", "OH"),
    },
    "Phe338": {
        "identity": ("A", 338, "PHE"),
        "aids": list(range(2567, 2574)),
        "atom_names": ("CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ"),
    },
    "Tyr341": {
        "identity": ("A", 341, "TYR"),
        "aids": list(range(2593, 2601)),
        "atom_names": ("CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ", "OH"),
    },
}

# Atom-specific distances are retained for direct comparison with CVSEQ.
GATE_AIDS = {
    "gate_Y124OH_F338CE2_A": (976, 2572),
    "gate_D74OD1_Y341OH_A": (562, 2600),
    "gate_D74OD2_Y341OH_A": (563, 2600),
    "gate_Y124OH_Y337OH_A": (976, 2562),
}

# Label-invariant side-chain minima complement, but do not replace, the exact
# atom-pair gate definitions above.
SIDECHAIN_GATE_PAIRS = {
    "gate_Y124_F338_sidechain_min_A": ("Tyr124", "Phe338"),
    "gate_D74_Y341_sidechain_min_A": ("Asp74", "Tyr341"),
    "gate_Y124_Y337_sidechain_min_A": ("Tyr124", "Tyr337"),
}

TYR337_TORSION_AIDS = {
    "Tyr337_chi1_deg": (2551, 2552, 2555, 2556),
    "Tyr337_chi2_deg": (2552, 2555, 2556, 2557),
}

OUTPUT_NAMES = {
    "timeseries": "critical_residue_distances.csv",
    "summary": "critical_residue_summary.csv",
    "metadata": "critical_residue_metadata.json",
}


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise argparse.ArgumentTypeError("value must be a finite number greater than zero")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be an integer greater than zero")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Calculate PBC-corrected ligand/side-chain distances for the eight "
            "requested AChE residues from saved Desmond trajectory frames."
        )
    )
    parser.add_argument("--cms", required=True, type=Path, help="Production *-out.cms")
    parser.add_argument(
        "--trajectory",
        required=True,
        type=Path,
        help="Production DTR directory (or another Desmond-readable trajectory)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("critical_residue_analysis"),
        help="Output directory (default: critical_residue_analysis)",
    )
    parser.add_argument(
        "--contact-cutoff",
        type=positive_float,
        default=DEFAULT_CONTACT_CUTOFF_A,
        metavar="ANGSTROM",
        help="Heavy-atom contact cutoff used for occupancy (default: 4.0 A)",
    )
    parser.add_argument(
        "--stride",
        type=positive_int,
        default=1,
        help="Analyze every Nth saved trajectory frame (default: 1)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace this script's existing output files in the output directory",
    )
    parser.add_argument("--version", action="version", version=SCRIPT_VERSION)
    return parser.parse_args()


def normalized_atom_identity(atom) -> tuple[str, int, str, str, int]:
    return (
        str(atom.chain).strip(),
        int(atom.resnum),
        str(atom.pdbres).strip(),
        str(atom.pdbname).strip(),
        int(atom.atomic_number),
    )


def validate_atom_identities(cms_model) -> None:
    atoms = cms_model.fsys_ct.atom
    for aid in LIGAND_AIDS:
        chain_name, resnum, resname, atom_name, atomic_number = normalized_atom_identity(
            atoms[aid]
        )
        if (chain_name, resnum, resname) != LIGAND_IDENTITY:
            raise RuntimeError(
                f"Ligand AID {aid} identity mismatch: "
                f"{chain_name}:{resname}{resnum}:{atom_name}"
            )
        if atomic_number <= 1:
            raise RuntimeError(f"Ligand AID {aid} is not a heavy atom")
    if len(set(LIGAND_AIDS)) != 34:
        raise RuntimeError(
            "The expected 34-atom G2N/LIG1 heavy-atom selection is incomplete"
        )

    for label, spec in RESIDUE_SPECS.items():
        observed_names = []
        for aid in spec["aids"]:
            chain_name, resnum, resname, atom_name, atomic_number = normalized_atom_identity(
                atoms[aid]
            )
            if (chain_name, resnum, resname) != spec["identity"]:
                raise RuntimeError(
                    f"{label} AID {aid} identity mismatch: "
                    f"{chain_name}:{resname}{resnum}:{atom_name}"
                )
            if atomic_number <= 1:
                raise RuntimeError(f"{label} AID {aid} is not a heavy atom")
            observed_names.append(atom_name)
        if tuple(observed_names) != spec["atom_names"]:
            raise RuntimeError(
                f"{label} atom-name mapping mismatch: observed {observed_names}, "
                f"expected {list(spec['atom_names'])}"
            )


def aids_to_checked_gids(cms_model, label: str, aids: Iterable[int]) -> list[int]:
    aid_list = list(aids)
    gids = topo.aids2gids(cms_model, aid_list, include_pseudoatoms=False)
    if len(gids) != len(aid_list) or len(set(gids)) != len(aid_list):
        raise RuntimeError(
            f"AID/GID conversion failed for {label}: "
            f"{len(aid_list)} AIDs produced {len(gids)} GIDs"
        )
    return gids


def frame_pbc(frame) -> analysis.Pbc:
    box = np.asarray(frame.box, dtype=np.float64)
    if box.shape == (3,):
        box = np.diag(box)
    if box.shape != (3, 3) or not np.isfinite(box).all():
        raise RuntimeError(f"Invalid trajectory box with shape {box.shape}")
    return analysis.Pbc(box)


def min_pair_distance(pos_a: np.ndarray, pos_b: np.ndarray, pbc: analysis.Pbc) -> float:
    from_pos = np.repeat(np.asarray(pos_a, dtype=np.float64), len(pos_b), axis=0)
    to_pos = np.tile(np.asarray(pos_b, dtype=np.float64), (len(pos_a), 1))
    delta = pbc.calcMinimumDiff(from_pos, to_pos)
    return float(np.linalg.norm(delta, axis=1).min())


def atom_distance(pos_a: np.ndarray, pos_b: np.ndarray, pbc: analysis.Pbc) -> float:
    from_pos = np.asarray(pos_a, dtype=np.float64).reshape(1, 3)
    to_pos = np.asarray(pos_b, dtype=np.float64).reshape(1, 3)
    return float(np.linalg.norm(pbc.calcMinimumDiff(from_pos, to_pos)[0]))


def dihedral_degrees(positions: np.ndarray, pbc: analysis.Pbc) -> float:
    p0, p1, p2, p3 = np.asarray(positions, dtype=np.float64)
    b0 = pbc.calcMinimumDiff(p1.reshape(1, 3), p0.reshape(1, 3))[0]
    b1 = pbc.calcMinimumDiff(p1.reshape(1, 3), p2.reshape(1, 3))[0]
    b2 = pbc.calcMinimumDiff(p2.reshape(1, 3), p3.reshape(1, 3))[0]
    b1_norm = np.linalg.norm(b1)
    if b1_norm <= 1.0e-12:
        raise RuntimeError("Cannot calculate a torsion with a zero-length central bond")
    b1_unit = b1 / b1_norm
    v = b0 - np.dot(b0, b1_unit) * b1_unit
    w = b2 - np.dot(b2, b1_unit) * b1_unit
    angle = np.degrees(np.arctan2(np.dot(np.cross(b1_unit, v), w), np.dot(v, w)))
    return float(angle)


def format_float(value: float) -> str:
    return f"{value:.6f}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def temporary_path(output_dir: Path, final_name: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix=f".{final_name}.", suffix=".tmp", dir=output_dir, delete=False
    )
    path = Path(handle.name)
    handle.close()
    return path


def write_summary(
    path: Path,
    values: dict[str, list[float]],
    residue_metrics: set[str],
    contact_cutoff: float,
) -> None:
    fields = [
        "metric", "kind", "unit", "statistics_method", "n_frames", "contact_cutoff_A",
        "occupancy_fraction", "occupancy_percent", "min", "mean", "sample_sd",
        "median", "p05", "p95", "max",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for metric, metric_values in values.items():
            data = np.asarray(metric_values, dtype=np.float64)
            is_residue = metric in residue_metrics
            is_torsion = metric.endswith("_deg")
            if is_torsion:
                angles = np.deg2rad(data)
                mean_vector = np.mean(np.exp(1.0j * angles))
                resultant_length = min(1.0, max(0.0, float(abs(mean_vector))))
                circular_mean = float(np.degrees(np.angle(mean_vector)))
                circular_sd = (
                    float(np.degrees(np.sqrt(-2.0 * np.log(resultant_length))))
                    if resultant_length > 0.0 else float("inf")
                )
            else:
                circular_mean = float("nan")
                circular_sd = float("nan")
            row = {
                "metric": metric,
                "kind": "ligand_sidechain_minimum" if is_residue else (
                    "torsion" if is_torsion else "gate"
                ),
                "unit": "degree" if is_torsion else "Angstrom",
                "statistics_method": "circular" if is_torsion else "linear",
                "n_frames": len(data),
                "contact_cutoff_A": format_float(contact_cutoff) if is_residue else "",
                "occupancy_fraction": (
                    format_float(float(np.mean(data <= contact_cutoff))) if is_residue else ""
                ),
                "occupancy_percent": (
                    format_float(float(100.0 * np.mean(data <= contact_cutoff)))
                    if is_residue else ""
                ),
                "min": "" if is_torsion else format_float(float(np.min(data))),
                "mean": format_float(circular_mean if is_torsion else float(np.mean(data))),
                "sample_sd": (
                    format_float(circular_sd) if is_torsion else (
                        format_float(float(np.std(data, ddof=1))) if len(data) > 1 else ""
                    )
                ),
                "median": "" if is_torsion else format_float(float(np.median(data))),
                "p05": "" if is_torsion else format_float(float(np.percentile(data, 5.0))),
                "p95": "" if is_torsion else format_float(float(np.percentile(data, 95.0))),
                "max": "" if is_torsion else format_float(float(np.max(data))),
            }
            writer.writerow(row)


def main() -> None:
    args = parse_args()
    cms_path = args.cms.expanduser().resolve()
    trajectory_path = args.trajectory.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not cms_path.is_file():
        raise SystemExit(f"ERROR: CMS file does not exist: {cms_path}")
    if not trajectory_path.exists():
        raise SystemExit(f"ERROR: trajectory does not exist: {trajectory_path}")
    if output_dir.exists() and not output_dir.is_dir():
        raise SystemExit(f"ERROR: output path is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    final_paths = {key: output_dir / name for key, name in OUTPUT_NAMES.items()}
    existing = [path for path in final_paths.values() if path.exists()]
    if existing and not args.force:
        joined = ", ".join(str(path) for path in existing)
        raise SystemExit(f"ERROR: output exists (use --force to replace): {joined}")

    _msys_model, cms_model = topo.read_cms(str(cms_path))
    validate_atom_identities(cms_model)

    ligand_gids = aids_to_checked_gids(cms_model, "LIG1 heavy atoms", LIGAND_AIDS)
    residue_gids = {
        name: aids_to_checked_gids(cms_model, name, spec["aids"])
        for name, spec in RESIDUE_SPECS.items()
    }
    gate_gids = {
        name: aids_to_checked_gids(cms_model, name, aids)
        for name, aids in GATE_AIDS.items()
    }
    torsion_gids = {
        name: aids_to_checked_gids(cms_model, name, aids)
        for name, aids in TYR337_TORSION_AIDS.items()
    }

    frame_source = traj.read_traj(str(trajectory_path), return_iter=True)
    frame_iterator = iter(frame_source)
    try:
        first_frame = next(frame_iterator)
    except StopIteration as exc:
        raise SystemExit(f"ERROR: no frames found in trajectory: {trajectory_path}") from exc

    consistency_error = topo.check_consistency(cms_model, first_frame)
    if consistency_error is not None:
        raise SystemExit(f"ERROR: CMS and trajectory are inconsistent: {consistency_error}")

    residue_metric_names = {
        name: f"min_lig_{name}_sidechain_A" for name in RESIDUE_SPECS
    }
    contact_names = {name: f"contact_lig_{name}_sidechain" for name in RESIDUE_SPECS}
    d74_min_name = "gate_D74Omin_Y341OH_A"
    metric_names = [*residue_metric_names.values(), *GATE_AIDS, d74_min_name]
    metric_names.extend(SIDECHAIN_GATE_PAIRS)
    metric_names.extend(TYR337_TORSION_AIDS)
    accumulated = {name: [] for name in metric_names}

    fieldnames = ["frame_index", "time_ps"]
    for residue_name in RESIDUE_SPECS:
        fieldnames.extend((residue_metric_names[residue_name], contact_names[residue_name]))
    fieldnames.extend(GATE_AIDS)
    fieldnames.append(d74_min_name)
    fieldnames.extend(SIDECHAIN_GATE_PAIRS)
    fieldnames.extend(TYR337_TORSION_AIDS)

    temp_paths = {
        key: temporary_path(output_dir, final_path.name)
        for key, final_path in final_paths.items()
    }
    total_frames_seen = 0
    analyzed_frames = 0
    first_time_ps = None
    last_time_ps = None
    previous_time_ps = None

    try:
        with temp_paths["timeseries"].open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for frame_index, frame in enumerate(chain((first_frame,), frame_iterator)):
                total_frames_seen += 1
                time_ps = float(frame.time)
                if not math.isfinite(time_ps):
                    raise RuntimeError(f"Non-finite time at trajectory frame {frame_index}")
                if previous_time_ps is not None and time_ps <= previous_time_ps:
                    raise RuntimeError(
                        f"Trajectory times are not strictly increasing at frame {frame_index}: "
                        f"{previous_time_ps} -> {time_ps} ps"
                    )
                previous_time_ps = time_ps
                if frame_index % args.stride:
                    continue

                pbc = frame_pbc(frame)
                ligand_pos = np.asarray(frame.pos(ligand_gids), dtype=np.float64)
                residue_pos = {
                    name: np.asarray(frame.pos(gids), dtype=np.float64)
                    for name, gids in residue_gids.items()
                }
                if not np.isfinite(ligand_pos).all() or any(
                    not np.isfinite(pos).all() for pos in residue_pos.values()
                ):
                    raise RuntimeError(f"Non-finite coordinates at trajectory frame {frame_index}")

                row: dict[str, str | int] = {
                    "frame_index": frame_index,
                    "time_ps": format_float(time_ps),
                }
                for residue_name in RESIDUE_SPECS:
                    metric_name = residue_metric_names[residue_name]
                    distance = min_pair_distance(
                        ligand_pos, residue_pos[residue_name], pbc
                    )
                    accumulated[metric_name].append(distance)
                    row[metric_name] = format_float(distance)
                    row[contact_names[residue_name]] = int(distance <= args.contact_cutoff)

                exact_gate_values = {}
                for gate_name, gids in gate_gids.items():
                    positions = np.asarray(frame.pos(gids), dtype=np.float64)
                    value = atom_distance(positions[0], positions[1], pbc)
                    exact_gate_values[gate_name] = value
                    accumulated[gate_name].append(value)
                    row[gate_name] = format_float(value)

                d74_min = min(
                    exact_gate_values["gate_D74OD1_Y341OH_A"],
                    exact_gate_values["gate_D74OD2_Y341OH_A"],
                )
                accumulated[d74_min_name].append(d74_min)
                row[d74_min_name] = format_float(d74_min)

                for gate_name, (first_residue, second_residue) in SIDECHAIN_GATE_PAIRS.items():
                    value = min_pair_distance(
                        residue_pos[first_residue], residue_pos[second_residue], pbc
                    )
                    accumulated[gate_name].append(value)
                    row[gate_name] = format_float(value)

                for torsion_name, gids in torsion_gids.items():
                    value = dihedral_degrees(
                        np.asarray(frame.pos(gids), dtype=np.float64), pbc
                    )
                    accumulated[torsion_name].append(value)
                    row[torsion_name] = format_float(value)

                writer.writerow(row)
                analyzed_frames += 1
                if first_time_ps is None:
                    first_time_ps = time_ps
                last_time_ps = time_ps

        if analyzed_frames == 0:
            raise RuntimeError("No frames remained after applying the trajectory stride")

        write_summary(
            temp_paths["summary"],
            accumulated,
            set(residue_metric_names.values()),
            args.contact_cutoff,
        )

        metadata = {
            "script": Path(__file__).name,
            "script_version": SCRIPT_VERSION,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "cms": str(cms_path),
            "cms_sha256": sha256_file(cms_path),
            "trajectory": str(trajectory_path),
            "contact_cutoff_A": args.contact_cutoff,
            "stride_saved_frames": args.stride,
            "total_frames_seen": total_frames_seen,
            "frames_analyzed": analyzed_frames,
            "first_analyzed_time_ps": first_time_ps,
            "last_analyzed_time_ps": last_time_ps,
            "ligand_AIDs": LIGAND_AIDS,
            "residue_sidechain_heavy_AIDs": {
                name: spec["aids"] for name, spec in RESIDUE_SPECS.items()
            },
            "exact_gate_AIDs": {name: list(aids) for name, aids in GATE_AIDS.items()},
            "Tyr337_torsion_AIDs": {
                name: list(aids) for name, aids in TYR337_TORSION_AIDS.items()
            },
            "distance_method": (
                "schrodinger.application.desmond.packages.analysis.Pbc.calcMinimumDiff"
            ),
            "production_overhead": "none; analysis uses saved trajectory frames",
        }
        temp_paths["metadata"].write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        for key in ("timeseries", "summary", "metadata"):
            temp_paths[key].replace(final_paths[key])
    except BaseException:
        for temp_path in temp_paths.values():
            temp_path.unlink(missing_ok=True)
        raise

    print(
        f"PASS: analyzed {analyzed_frames} of {total_frames_seen} saved frames "
        f"(stride={args.stride})."
    )
    for path in final_paths.values():
        print(f"Wrote: {path}")


if __name__ == "__main__":
    main()
