"""Regenerates tests/fixtures/sample_part.step.

Run with: python tests/fixtures/generate_sample_part.py

A 60x40x15mm plate with a through-hole, a countersunk hole, and a slot --
enough feature variety to exercise plane/cylinder/cone/slot-pairing/
standalone-circle extraction in cad_parser without needing a real customer
CAD file checked into the repo.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq


def build_sample_part() -> cq.Workplane:
    return (
        cq.Workplane("XY")
        .box(60, 40, 15)
        .faces(">Z")
        .workplane()
        .pushPoints([(15, 0)])
        .hole(8)
        .faces(">Z")
        .workplane()
        .pushPoints([(-15, 10)])
        .cskHole(6, 12, 90)
        .faces(">Z")
        .workplane()
        .pushPoints([(-15, -10)])
        .slot2D(20, 6, 0)
        .cutBlind(-15)
    )


if __name__ == "__main__":
    out_path = Path(__file__).parent / "sample_part.step"
    cq.exporters.export(build_sample_part(), str(out_path))
    print(f"Wrote {out_path}")
