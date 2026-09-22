# CMM-Gen

Ingests a 3D CAD `.STEP` file and a blueprint (PDF/image) to automatically
generate production-ready PC-DMIS inspection routines.

## Status

All four core modules are implemented and tested end-to-end against a
synthetic STEP fixture (plate + through-hole + countersink + slot):
STEP parsing, Claude-Vision GD&T extraction/matching, kinematic
reachability validation, and native PC-DMIS BASIC generation. 27 tests pass
(`pytest`), `ruff` is clean, and `mypy --strict` is clean.

**Before trusting output on real parts**, replace the placeholder specs in
`config/probe_library.yaml` and `config/machine_envelope.yaml` with your
actual probe/machine datasheet values (both are loaded with a loud
`verified: false` warning until you do), and review the `$$ VERIFY` comments
`pcdmis_generator.py` emits for the less-common DIM types and the Cypress
Enable BASIC COM object names -- see **Conventions** below for what's
confidently sourced vs. what needs a check against your PC-DMIS install.

## Architecture

```
cmm_gen/
  models.py                Shared data contracts (CADFeature, GDTCallout,
                            ProbeConfig, ApproachVector, InspectionRoutine, ...)
  cad_parser.py             .STEP -> list[CADFeature]        (CadQuery/OCP)
  gdt_extractor.py          blueprint -> list[GDTCallout]     (Claude Vision)
  kinematics_validator.py   feature + probe + envelope -> ApproachVector
  pcdmis_generator.py       InspectionRoutine -> .PRG file    (PC-DMIS BASIC)
  cli.py                    Typer CLI (parse-cad / extract-gdt / generate / visualize)
  visualizer/app.py         Streamlit verification UI (3D feature + approach-vector plots)
config/
  probe_library.yaml        Named probe configs (HH-A-T5, HA-TM-31, HP-TBe, HT-TM-MF)
  machine_envelope.yaml     Named machine travel envelopes + fixture keep-out boxes
tests/                      pytest suite, incl. a generated STEP fixture
```

Data flows strictly left-to-right through `models.py` types: `cad_parser`
never imports `gdt_extractor`, `gdt_extractor` never imports
`kinematics_validator`, etc. Each module is developed and unit-tested in
isolation against the shared schema.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...   # required for gdt_extractor
```

## CLI

```bash
python -m cmm_gen.cli parse-cad --step part.step
python -m cmm_gen.cli extract-gdt --blueprint part.pdf --step part.step
python -m cmm_gen.cli generate --step part.step --blueprint part.pdf --out part.prg \
    --probe HH-A-T5 --machine default
python -m cmm_gen.cli visualize
```

`generate` runs the full pipeline (parse -> extract & map GD&T -> validate
kinematics -> emit PC-DMIS) and prints a report of any unreachable features
or unmapped callouts it excluded, rather than silently dropping them. ASME
Y14.5 alignment needs a primary/secondary/tertiary datum identified on the
blueprint (a datum feature symbol Claude can read as `defines_datum` on a
callout); if fewer than three are found, the CLI stops and tells you to
build the `InspectionRoutine` via the Python API with explicit datums
instead.

## Tests

```bash
pytest                    # 27 tests
ruff check cmm_gen/ tests/
mypy cmm_gen/              # --strict, per pyproject.toml
```

`tests/fixtures/sample_part.step` (regenerate with
`python tests/fixtures/generate_sample_part.py`) is a synthetic plate with a
through-hole, a countersunk hole, and a slot -- enough feature variety to
exercise plane/cylinder/cone/slot-pairing/standalone-circle extraction,
kinematic validation (including a genuinely unreachable underside face), and
DIM-block generation without needing a real customer CAD file in the repo.

## Conventions

Answers to the clarifying questions this project's spec asked for, before
the gated modules were implemented:

1. **PC-DMIS output format & version**: native PC-DMIS BASIC (`.PRG`),
   targeting current (2023.x/2024.x) command-language syntax.
2. **Probe hardware**: `config/probe_library.yaml` defines four named
   configs -- `HH-A-T5`, `HA-TM-31`, `HP-TBe`, `HT-TM-MF` -- with
   PLACEHOLDER tip/stylus/index-angle specs (typical Renishaw PH10-style
   indexable-head defaults) pending your datasheet values.
   `config/machine_envelope.yaml` likewise ships one PLACEHOLDER machine
   with generous travel limits and no fixture keep-out boxes.
3. **GD&T convention**: ASME Y14.5 -- primary/secondary/tertiary datum
   precedence (A|B|C), MMC/LMC/RFS material condition modifiers.

### What's confidently sourced vs. what to verify

`pcdmis_generator.py` follows well-documented PC-DMIS command-language
conventions for alignment (`ALIGNMENT/LEVEL`, `ROTATE`, `ORIGIN`), feature
definition (`FEAT/...`, `THEO`/`ACTL`/`TARG`), moves (`MOVE/POINT`), and the
common DIM types (`TRUEPOS`, `FLATNESS`, `PERPENDICULARITY`, `PARALLELISM`,
`ANGULARITY`, `CIRCULARITY`, `CYLINDRICITY`, `RUNOUT`, `STRAIGHTNESS`,
`LOCATION`). Less-common DIM types (`PROFILE`, `TOTAL RUNOUT`,
`CONCENTRICITY`, `SYMMETRY`) and the exact PC-DMIS Basic COM object/property
names used in the generated Cypress Enable BASIC tolerance-check subroutine
are marked inline with `$$ VERIFY` / `'$$ VERIFY` comments in the generated
`.PRG` -- this project doesn't have access to a live PC-DMIS instance to
validate exact syntax against, so confirm those against your installation
before running on the shop floor.

### Known simplifications (documented in-code, not silent)

- **Collision model**: `kinematics_validator.check_collision` is a
  bounding-box proxy (clearance-point-vs-part-bbox for blind spots,
  swept-path-vs-keepout-box for fixture collisions), not a full
  solid-vs-solid sweep against the actual STEP geometry.
- **Hit-pattern placement**: `pcdmis_generator._feature_hit_points` generates
  a reasonable default hit pattern per feature type (e.g. 3 points on a
  plane, 5 around a circle, 2 levels x 4 around a cylinder); real hit
  placement should respect the actual face boundary, which the visualizer's
  "verify before export" view is for.
- **Slot reconstruction**: `cad_parser` pairs half-cylindrical end-cap faces
  into a SLOT feature heuristically (matching radius + parallel axis +
  nearest distance); unusual slot geometry may not pair up and surfaces as a
  logged warning rather than a silently wrong feature.
