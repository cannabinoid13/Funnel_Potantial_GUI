# Test corpus

Real Desmond run packages, kept in the repository so the suites do not depend
on anything outside it.

## `ache/`

`AChE-G2N_WT_Funnel_MetaD_run` — a well-tempered funnel-metadynamics package
for acetylcholinesterase with the G2N ligand, produced by a different toolkit
from this one. It is the general regression fixture: none of its groups is
called `lig`/`site`/`core` (the frame is `frame_o`/`frame_u`/`frame_v`), its
atom numbering is its own, and the interface was never written against it.

Extracted from the supplied ZIP with `funnelforge.core.safety.safe_extract_zip`,
which is itself part of what the suites test.

| file | what it is |
|---|---|
| `*.pot` | 108 statements, 18 atom selections, one accumulator, well-tempered |
| `*.cms` | 41 986 atoms |
| `*.msj`, `*.cfg`, `*.sh` | the run package the potential belongs to |
| the rest | the producing toolkit's own docs and scripts, kept as they came |

## `generated/`

Built on demand by `tests/conftest_fixtures.py` from the `.cms` above: a
designer-shaped job (`lig`/`site`/`core`, a fitted funnel, a standard `.msj`
and `.cfg`). It exists so `tests/test_funnelforge.py`, which tests the funnel
*designer* and therefore needs the designer's own template, does not depend on
any file outside the repository. Delete the directory to have it rebuilt.
