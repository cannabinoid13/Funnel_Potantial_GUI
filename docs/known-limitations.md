# Current compatibility and limits

Updated 2026-09-05. The earlier audit described the template-only designer;
that import/export path has now been replaced for custom cylindrical funnels.

## What is now supported

- ZIP packages, directories and individual Desmond `.pot/.cms/.msj/.cfg/.sh`
  files. References in the stage chain and launcher identify companion files;
  ambiguous bundles are reported instead of mixing systems.
- Protein-attached custom funnel frames, differently named symbols, custom
  radius and wall expressions, additional energy contributions and monitoring
  expressions. The source remains authoritative. Edits replace relevant
  source spans and preserve unrelated expressions, comments and line endings.
- The existing 3D position/tilt controls and numerical panels operate on the
  recovered frame. Custom energy shells, cross-sections and volume sampling
  use the imported energy expression rather than assuming harmonic walls.
- The real AChE-G2N corpus and supplied AChE-G6N/BChE-G4N ZIP packages have
  been exercised through import, edits and numerical verification. Edited
  custom source was also accepted by the installed Desmond 2025-3 parser.
- Exports retain package dependencies and update file references and checksum
  manifests. A frozen original-geometry validator is retained as provenance;
  adjusted exports use a current-artifact validator and retain the original
  engine preflight. Old scientific validation claims are not transferred to
  the new geometry.
- Failed verification, invalid geometry or missing launch dependencies prevents
  committing a partial job directory. Imported source files are not overwritten.
- The source workbench exports the exact analyzed revision, including its
  encoding, and rejects stale source/topology validation evidence. Source save
  retains a UTF-8 BOM; run-ready export removes it because Desmond rejects BOMs.

## Boundaries that remain

The automatic 3D bridge recognizes a cylindrical coordinate derived from a
ligand group centre projected onto a protein-attached axis, together with a
radial profile. It does not infer a unique 3D funnel from an arbitrary CV
(e.g. RMSD-only bias, multiple unrelated funnels or an opaque engine extension).
Such source can still be inspected and saved in the general source workbench;
unsupported edits must be reported rather than replaced with template physics.
The standard cylindrical CV can switch between one and two dimensions;
other CV transformations require editing the corresponding expressions in source.

The local expression evaluator is a preview of Desmond's language, not the
engine. Valid engine functions outside its implemented numerical subset may
be preserved but cannot be certified numerically by the preview. The official
Desmond parser remains authoritative for engine compatibility. A parse pass
is not a production trajectory or a convergence assessment.

Numerical verification evaluates the initial configuration and spatial probe
positions, with initially zero accumulated metadynamics bias. It does not
simulate a hill history, protein flexibility or a long trajectory.

The local ASL reader is not a complete Maestro ASL implementation. Explicit
atom lists are checked strictly, local selections use consistent 1-based
identifiers in documents, and unresolved selections are reported. Official
validation against the exact CMS is needed for engine-specific ASL semantics.

The source workbench's semantic, physical and package reports still do not
implement every catalogued diagnostic. Designer export performs its own
cross-file and geometry checks. Read the actual diagnostics and validation
records rather than treating an absence of warnings as evidence of convergence.

The bundled official adapter has been exercised with Schrödinger 2025-3 on
Linux. Other installed releases can return their own parser verdict, but
version-specific diagnostics and native Windows behavior are not fully tested.
