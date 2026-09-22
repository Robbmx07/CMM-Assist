"""CMM-Gen command-line interface.

Usage:
    python -m cmm_gen.cli generate --step part.step --blueprint part.pdf --out part.prg
    python -m cmm_gen.cli parse-cad --step part.step
    python -m cmm_gen.cli extract-gdt --blueprint part.pdf --step part.step
    python -m cmm_gen.cli visualize --step part.step --blueprint part.pdf
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from cmm_gen.logging_config import configure_logging

app = typer.Typer(help="CMM-Gen: STEP + blueprint -> PC-DMIS inspection routine.")
console = Console()


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    configure_logging(verbose=verbose)


@app.command("parse-cad")
def parse_cad(step: Path = typer.Option(..., exists=True, help="Path to .STEP file")) -> None:
    """Parse a STEP file and print the extracted features."""
    from cmm_gen import cad_parser

    features = cad_parser.parse_step_file(step)
    table = Table(title=f"Features in {step.name}")
    table.add_column("ID")
    table.add_column("Type")
    table.add_column("Location")
    table.add_column("Diameter")
    for f in features:
        table.add_row(
            f.id, f.type.value, str(f.nominal_location.as_tuple()), str(f.diameter or "-")
        )
    console.print(table)


@app.command("extract-gdt")
def extract_gdt(
    blueprint: Path = typer.Option(..., exists=True),
    step: Path = typer.Option(..., exists=True),
) -> None:
    """Extract GD&T callouts from a blueprint and map them to CAD features."""
    from cmm_gen import cad_parser, gdt_extractor

    features = cad_parser.parse_step_file(step)
    callouts = gdt_extractor.extract_and_map(blueprint, features)
    table = Table(title=f"GD&T callouts in {blueprint.name}")
    table.add_column("ID")
    table.add_column("Characteristic")
    table.add_column("Tolerance")
    table.add_column("Datums")
    table.add_column("Mapped Feature")
    for c in callouts:
        table.add_row(
            c.id,
            c.characteristic.value,
            str(c.tolerance_value),
            ",".join(d.label for d in c.datums),
            c.mapped_feature_id or "[red]UNMAPPED[/red]",
        )
    console.print(table)


@app.command("generate")
def generate(
    step: Path = typer.Option(..., exists=True),
    blueprint: Path = typer.Option(..., exists=True),
    out: Path = typer.Option(..., help="Output .PRG path"),
    probe: str = typer.Option("HH-A-T5", help="Probe name from config/probe_library.yaml"),
    machine: str = typer.Option("default", help="Machine name from config/machine_envelope.yaml"),
) -> None:
    """Full pipeline: parse -> extract GD&T -> validate kinematics -> generate PC-DMIS.

    Only reachable features and mapped GD&T callouts make it into the
    generated routine -- unreachable features and unmapped callouts are
    printed as a report rather than silently dropped, per the spec's error
    handling requirement.
    """
    from cmm_gen import cad_parser, gdt_extractor, kinematics_validator, pcdmis_generator
    from cmm_gen.models import ApproachVectorStatus, DatumFeature, InspectionRoutine

    shape = cad_parser.load_step_file(step)
    features = cad_parser.extract_features(shape)
    part_bbox = cad_parser.compute_bounding_box(shape)
    console.print(f"Extracted {len(features)} features from {step.name}")

    callouts = gdt_extractor.extract_and_map(blueprint, features)
    mapped_callouts = [c for c in callouts if c.mapped_feature_id is not None]
    unmapped_callouts = [c for c in callouts if c.mapped_feature_id is None]
    console.print(
        f"Extracted {len(callouts)} GD&T callout(s) from {blueprint.name} "
        f"({len(mapped_callouts)} mapped, {len(unmapped_callouts)} unmapped)"
    )
    if unmapped_callouts:
        table = Table(title="Unmapped GD&T callouts (excluded from routine)")
        table.add_column("Callout")
        table.add_column("Characteristic")
        for c in unmapped_callouts:
            table.add_row(c.id, c.characteristic.value)
        console.print(table)

    probe_config = kinematics_validator.load_probe_config(probe)
    envelope = kinematics_validator.load_machine_envelope(machine)

    all_vectors = [
        kinematics_validator.validate_feature(f, probe_config, envelope, part_bbox)
        for f in features
    ]
    reachable_ids = {
        av.feature_id for av in all_vectors if av.status == ApproachVectorStatus.VALID
    }
    unreachable = [av for av in all_vectors if av.feature_id not in reachable_ids]
    if unreachable:
        table = Table(title="Unreachable features (excluded from routine)")
        table.add_column("Feature")
        table.add_column("Status")
        table.add_column("Reason")
        for av in unreachable:
            table.add_row(av.feature_id, av.status.value, av.rejection_reason or "")
        console.print(table)

    reachable_features = [f for f in features if f.id in reachable_ids]
    reachable_vectors = [av for av in all_vectors if av.feature_id in reachable_ids]

    datum_labels = sorted({c.defines_datum for c in mapped_callouts if c.defines_datum})
    if len(datum_labels) < 3:
        console.print(
            f"[red]Only found {len(datum_labels)} datum feature(s) {datum_labels} on the "
            "blueprint; ASME Y14.5 alignment needs a primary/secondary/tertiary datum. "
            "Build the InspectionRoutine via the Python API with explicit datums instead "
            "of this CLI shortcut.[/red]"
        )
        raise typer.Exit(code=1)

    def _datum_feature_id(label: str) -> str:
        callout = next(c for c in mapped_callouts if c.defines_datum == label)
        assert callout.mapped_feature_id is not None  # guaranteed: filtered to mapped_callouts
        return callout.mapped_feature_id

    datums = [
        DatumFeature(label=label, feature_id=_datum_feature_id(label), precedence=precedence)
        for precedence, label in enumerate(datum_labels[:3], start=1)
    ]

    routine = InspectionRoutine(
        part_name=step.stem,
        features=reachable_features,
        callouts=mapped_callouts,
        datums=datums,
        approach_vectors=reachable_vectors,
        probe_config=probe_config,
        machine_envelope=envelope,
    )

    try:
        pcdmis_generator.generate_routine(routine, out)
    except pcdmis_generator.RoutineGenerationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Wrote {out}[/green]")


@app.command("visualize")
def visualize() -> None:
    """Launch the local Streamlit visualizer for feature extraction and
    probe approach trajectories."""
    import subprocess
    import sys

    app_path = Path(__file__).parent / "visualizer" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app_path)], check=True)


if __name__ == "__main__":
    app()
