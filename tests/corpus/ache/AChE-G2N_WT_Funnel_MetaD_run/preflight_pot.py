#!/usr/bin/env python3
"""Resolve atom groups and compile the POT with the installed Desmond parser."""

from __future__ import annotations

from pathlib import Path
import sys


GROUPS = {
    "G2N ligand heavy": (
        8286, 8287, 8288, 8289, 8290, 8291, 8292, 8293, 8294, 8295,
        8296, 8297, 8298, 8299, 8300, 8301, 8302, 8303, 8304, 8305,
        8306, 8307, 8308, 8309, 8310, 8311, 8312, 8313, 8314, 8315,
        8316, 8317, 8318, 8319,
    ),
    "PBC-safe frame O (399-405 backbone)": (
        3037, 3038, 3039, 3040, 3043, 3044, 3045, 3046, 3051, 3052,
        3053, 3054, 3058, 3059, 3060, 3061, 3065, 3066, 3067, 3068,
        3069, 3070, 3071, 3072, 3077, 3078, 3079, 3080,
    ),
    "PBC-safe frame U (528-533 backbone)": (
        4075, 4076, 4077, 4078, 4080, 4081, 4082, 4083, 4086, 4087,
        4088, 4089, 4091, 4092, 4093, 4094, 4102, 4103, 4104, 4105,
        4116, 4117, 4118, 4119,
    ),
    "PBC-safe frame V (424-430 backbone)": (
        3207, 3208, 3209, 3210, 3218, 3219, 3220, 3221, 3225, 3226,
        3227, 3228, 3237, 3238, 3239, 3240, 3242, 3243, 3244, 3245,
        3254, 3255, 3256, 3257, 3261, 3262, 3263, 3264,
    ),
    "Asp74 monitor": (
        560, 561, 562, 563,
    ),
    "Trp86 monitor": (
        654, 655, 656, 657, 658, 659, 660, 661, 662, 663,
    ),
    "Tyr124 monitor": (
        969, 970, 971, 972, 973, 974, 975, 976,
    ),
    "Trp286 monitor": (
        2160, 2161, 2162, 2163, 2164, 2165, 2166, 2167, 2168, 2169,
    ),
    "Phe297 monitor": (
        2259, 2260, 2261, 2262, 2263, 2264, 2265,
    ),
    "Tyr337 monitor": (
        2555, 2556, 2557, 2558, 2559, 2560, 2561, 2562,
    ),
    "Phe338 monitor": (
        2567, 2568, 2569, 2570, 2571, 2572, 2573,
    ),
    "Tyr341 monitor": (
        2593, 2594, 2595, 2596, 2597, 2598, 2599, 2600,
    ),
}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(f"Usage: {Path(sys.argv[0]).name} SYSTEM.cms POTENTIAL.pot")

    cms_path = Path(sys.argv[1]).expanduser().resolve()
    pot_path = Path(sys.argv[2]).expanduser().resolve()
    if not cms_path.is_file():
        raise SystemExit(f"ERROR: CMS file does not exist: {cms_path}")
    if not pot_path.is_file():
        raise SystemExit(f"ERROR: POT file does not exist: {pot_path}")

    try:
        from schrodinger.application.desmond import enhsamp
        from schrodinger.application.desmond.packages import topo
    except Exception as exc:
        raise RuntimeError("Could not import the installed Schrodinger Desmond API") from exc

    try:
        _msys_model, cms_model = topo.read_cms(str(cms_path))
    except Exception as exc:
        raise RuntimeError(f"Could not read CMS: {cms_path}") from exc

    for label, aids in GROUPS.items():
        asl = "atom.num " + ",".join(str(aid) for aid in aids)
        try:
            selected = tuple(sorted(cms_model.select_atom(asl)))
        except Exception as exc:
            raise RuntimeError(f"ASL resolution failed for {label}: {asl}") from exc
        if selected != aids:
            raise RuntimeError(
                f"AID resolution mismatch for {label}: expected {aids}, got {selected}"
            )

    try:
        m_expression = pot_path.read_text(encoding="utf-8")
        compiled = enhsamp.parseStr(cms_model, m_expression)
    except Exception as exc:
        raise RuntimeError(f"Desmond rejected the POT expression: {pot_path}") from exc
    if compiled is None:
        raise RuntimeError("The Desmond enhanced-sampling parser returned None")

    print("PASS: CMS loaded and all ligand/frame/monitor AIDs resolved exactly.")
    print("PASS: POT parsed and type-checked by the installed Desmond engine.")


if __name__ == "__main__":
    main()
