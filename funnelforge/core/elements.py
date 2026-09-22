"""Element data used for masses, radii and CPK colours.

Masses follow the values Schrodinger/OPLS writes into ``ffio_sites`` so that
centre-of-mass values computed here match what the Desmond backend computes
(e.g. C = 12.01115, N = 14.0067, O = 15.9994, S = 32.064).  Whenever the .cms
carries explicit force-field masses those are preferred; this table is the
fallback and is used for the periodic-table look-ups in the GUI.
"""

from __future__ import annotations

# Z -> (symbol, mass, vdw radius A, covalent radius A, (r, g, b) 0-1)
ELEMENTS: dict[int, tuple[str, float, float, float, tuple[float, float, float]]] = {
    0:  ("Xx", 0.00000, 1.00, 0.50, (0.85, 0.20, 0.85)),
    1:  ("H",  1.00794, 1.20, 0.31, (0.92, 0.92, 0.92)),
    2:  ("He", 4.00260, 1.40, 0.28, (0.85, 1.00, 1.00)),
    3:  ("Li", 6.94100, 1.82, 1.28, (0.80, 0.50, 1.00)),
    4:  ("Be", 9.01218, 1.53, 0.96, (0.76, 1.00, 0.00)),
    5:  ("B",  10.81100, 1.92, 0.84, (1.00, 0.71, 0.71)),
    6:  ("C",  12.01115, 1.70, 0.76, (0.35, 0.75, 0.42)),
    7:  ("N",  14.00670, 1.55, 0.71, (0.18, 0.31, 0.97)),
    8:  ("O",  15.99940, 1.52, 0.66, (1.00, 0.18, 0.18)),
    9:  ("F",  18.99840, 1.47, 0.57, (0.56, 0.88, 0.31)),
    10: ("Ne", 20.17970, 1.54, 0.58, (0.70, 0.89, 0.96)),
    11: ("Na", 22.98977, 2.27, 1.66, (0.67, 0.36, 0.95)),
    12: ("Mg", 24.30500, 1.73, 1.41, (0.54, 1.00, 0.00)),
    13: ("Al", 26.98154, 1.84, 1.21, (0.75, 0.65, 0.65)),
    14: ("Si", 28.08550, 2.10, 1.11, (0.94, 0.78, 0.63)),
    15: ("P",  30.97376, 1.80, 1.07, (1.00, 0.50, 0.00)),
    16: ("S",  32.06400, 1.80, 1.05, (1.00, 0.86, 0.19)),
    17: ("Cl", 35.45300, 1.75, 1.02, (0.12, 0.94, 0.12)),
    18: ("Ar", 39.94800, 1.88, 1.06, (0.50, 0.82, 0.89)),
    19: ("K",  39.09830, 2.75, 2.03, (0.56, 0.25, 0.83)),
    20: ("Ca", 40.08000, 2.31, 1.76, (0.24, 1.00, 0.00)),
    21: ("Sc", 44.95590, 2.11, 1.70, (0.90, 0.90, 0.90)),
    22: ("Ti", 47.88000, 2.00, 1.60, (0.75, 0.76, 0.78)),
    23: ("V",  50.94150, 2.00, 1.53, (0.65, 0.65, 0.67)),
    24: ("Cr", 51.99600, 2.00, 1.39, (0.54, 0.60, 0.78)),
    25: ("Mn", 54.93800, 2.00, 1.39, (0.61, 0.48, 0.78)),
    26: ("Fe", 55.84700, 2.00, 1.32, (0.88, 0.40, 0.20)),
    27: ("Co", 58.93320, 2.00, 1.26, (0.94, 0.56, 0.63)),
    28: ("Ni", 58.69340, 1.63, 1.24, (0.31, 0.82, 0.31)),
    29: ("Cu", 63.54600, 1.40, 1.32, (0.78, 0.50, 0.20)),
    30: ("Zn", 65.39000, 1.39, 1.22, (0.49, 0.50, 0.69)),
    33: ("As", 74.92160, 1.85, 1.19, (0.74, 0.50, 0.89)),
    34: ("Se", 78.96000, 1.90, 1.20, (1.00, 0.63, 0.00)),
    35: ("Br", 79.90400, 1.85, 1.20, (0.65, 0.16, 0.16)),
    36: ("Kr", 83.80000, 2.02, 1.16, (0.36, 0.72, 0.82)),
    37: ("Rb", 85.46780, 3.03, 2.20, (0.44, 0.18, 0.69)),
    38: ("Sr", 87.62000, 2.49, 1.95, (0.00, 1.00, 0.00)),
    47: ("Ag", 107.86820, 1.72, 1.45, (0.75, 0.75, 0.75)),
    48: ("Cd", 112.41100, 1.58, 1.44, (1.00, 0.85, 0.56)),
    53: ("I",  126.90450, 1.98, 1.39, (0.58, 0.00, 0.58)),
    55: ("Cs", 132.90540, 3.43, 2.44, (0.34, 0.09, 0.56)),
    56: ("Ba", 137.32700, 2.68, 2.15, (0.00, 0.79, 0.00)),
    78: ("Pt", 195.08000, 1.75, 1.36, (0.82, 0.82, 0.88)),
    79: ("Au", 196.96654, 1.66, 1.36, (1.00, 0.82, 0.14)),
    80: ("Hg", 200.59000, 1.55, 1.32, (0.72, 0.72, 0.82)),
}

_DEFAULT = ("X", 12.0, 1.70, 0.75, (0.75, 0.30, 0.75))

SYMBOL_TO_Z = {v[0].upper(): k for k, v in ELEMENTS.items()}


def symbol(z: int) -> str:
    return ELEMENTS.get(z, _DEFAULT)[0]


def mass(z: int) -> float:
    return ELEMENTS.get(z, _DEFAULT)[1]


def vdw_radius(z: int) -> float:
    return ELEMENTS.get(z, _DEFAULT)[2]


def covalent_radius(z: int) -> float:
    return ELEMENTS.get(z, _DEFAULT)[3]


def color(z: int) -> tuple[float, float, float]:
    return ELEMENTS.get(z, _DEFAULT)[4]


# --------------------------------------------------------------------------
# Residue metadata (used for colouring and for the residue browser)
# --------------------------------------------------------------------------

AA3 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V", "SEC": "U", "PYL": "O",
    # Common protonation / cap variants written by Protein Prep
    "HID": "H", "HIE": "H", "HIP": "H", "HISD": "H", "HISE": "H", "HSD": "H",
    "HSE": "H", "HSP": "H", "CYX": "C", "CYM": "C", "ASH": "D", "GLH": "E",
    "LYN": "K", "ARN": "R", "TYM": "Y", "ACE": "X", "NMA": "X", "NME": "X",
}

NUCLEIC = {"A", "C", "G", "T", "U", "DA", "DC", "DG", "DT", "DU",
           "RA", "RC", "RG", "RU", "ADE", "CYT", "GUA", "THY", "URA"}

WATER_NAMES = {"HOH", "H2O", "WAT", "TIP3", "TIP4", "SPC", "T3P", "SOL",
               "DOD", "TIP", "T4P"}

ION_NAMES = {"NA", "CL", "K", "MG", "CA", "ZN", "MN", "FE", "CU", "LI",
             "BR", "I", "F", "CS", "RB", "SR", "BA", "CO", "NI", "CD",
             "SOD", "CLA", "POT", "NA+", "CL-", "K+", "MG2", "CA2", "ZN2"}

BACKBONE_NAMES = {"N", "CA", "C", "O", "OXT", "H", "HA", "HN"}
BACKBONE_HEAVY = {"N", "CA", "C", "O", "OXT"}

# Kyte-Doolittle-ish hydrophobicity classes used for the "residue type" colouring
RESIDUE_CLASS = {
    "hydrophobic": {"ALA", "VAL", "LEU", "ILE", "MET", "PHE", "TRP", "PRO", "GLY"},
    "polar": {"SER", "THR", "CYS", "ASN", "GLN", "TYR", "CYX"},
    "acidic": {"ASP", "GLU", "ASH", "GLH"},
    "basic": {"LYS", "ARG", "HIS", "HID", "HIE", "HIP", "HSD", "HSE", "HSP", "LYN"},
}

RESIDUE_CLASS_COLOR = {
    "hydrophobic": (0.55, 0.75, 0.45),
    "polar": (0.45, 0.70, 0.90),
    "acidic": (0.95, 0.40, 0.40),
    "basic": (0.40, 0.45, 0.95),
    "other": (0.75, 0.75, 0.75),
}


def residue_class(resname: str) -> str:
    rn = (resname or "").strip().upper()
    for cls, names in RESIDUE_CLASS.items():
        if rn in names:
            return cls
    return "other"


def three_to_one(resname: str) -> str:
    return AA3.get((resname or "").strip().upper(), "X")
