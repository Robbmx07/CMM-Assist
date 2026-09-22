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
    probe_config: Path = typer.Option(
        None, help="YAML probe/machine config; uses defaults if omitted"
    ),
) -> None:
    """Full pipeline: parse -> extract GD&T -> validate kinematics -> generate PC-DMIS."""
    console.print(
        "[yellow]generate: end-to-end pipeline wiring is pending the clarifying "
        "questions in the README (probe config, output format, datum "
        "conventions).[/yellow]"
    )
    raise typer.Exit(code=1)


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
