# CMM-Gen

Ingests a 3D CAD `.STEP` file and a blueprint (PDF/image) to automatically
generate production-ready PC-DMIS inspection routines.

## Status

Project scaffolding and shared data contracts (`cmm_gen/models.py`) are in
place. The four core modules are stubbed with full interfaces, docstrings,
and type hints, but the parts of their implementation that depend on
shop-specific standards are gated behind open questions -- see
**Open Questions** below. Answer those (in the PR description, an issue, or
directly to the assistant) before the complex logic in each module is
filled in.

## Architecture

```
cmm_gen/
  models.py                Shared data contracts (CADFeature, GDTCallout,
                            ProbeConfig, ApproachVector, InspectionRoutine, ...)
  cad_parser.py             .STEP -> list[CADFeature]        (CadQuery/OCP)
  gdt_extractor.py          blueprint -> list[GDTCallout]     (Claude Vision)
  kinematics_validator.py   feature + probe + envelope -> ApproachVector
  pcdmis_generator.py       InspectionRoutine -> .PRG file
  cli.py                    Typer CLI (parse-cad / extract-gdt / generate / visualize)
  visualizer/app.py         Streamlit verification UI
tests/                      pytest suite
```

Data flows strictly left-to-right through `models.py` types: `cad_parser`
never imports `gdt_extractor`, `gdt_extractor` never imports
`kinematics_validator`, etc. Each module can be developed and unit-tested in
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
python -m cmm_gen.cli generate --step part.step --blueprint part.pdf --out part.prg
python -m cmm_gen.cli visualize
```

## Open Questions

These need answers before `pcdmis_generator.py`, the collision/accessibility
rules in `kinematics_validator.py`, and the matching logic in
`gdt_extractor.py` can be implemented (rather than stubbed):

1. **PC-DMIS output format & version** -- native PC-DMIS BASIC command
   language (with Cypress Enable BASIC subroutines) vs. neutral DMIS
   (ISO 6983-style) output, and which PC-DMIS version's syntax/feature set to
   target.
2. **Probe library & hardware constraints** -- standard tip/stylus
   configurations (star probe layouts, extension lengths), head type
   (e.g. Renishaw PH10 fixed-index angles vs. PH20/REVO continuous), and the
   machine's travel envelope and fixture keep-out zones.
3. **Datum hierarchy & tolerance mapping conventions** -- ASME Y14.5 vs.
   ISO 1101 conventions, alignment strategy (3-2-1 vs. RPS), and how the shop
   wants composite/multi-datum frames represented.

See `cmm_gen/*.py` docstrings for exactly which functions are gated on each
question.
