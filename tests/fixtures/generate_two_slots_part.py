"""Regenerates tests/fixtures/two_slots_part.step.

Run with: python tests/fixtures/generate_two_slots_part.py

An 80x40x15mm plate with two parallel slots 40mm apart -- a regression
fixture for cad_parser's slot end-cap pairing: a naive "nearest from one
side" greedy match can cross-pair end-caps from two different slots when
they're closely spaced, silently producing two wrong slots instead of two
right ones. This fixture has two slots close enough together that a wrong
greedy pairing is plausible, so the mutual-nearest-neighbor pairing logic
has something real to get right.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq


def build_two_slots_part() -> cq.Workplane:
    part = (
        cq.Workplane("XY")
        .box(80, 40, 15)
        .faces(">Z")
        .workplane()
        .pushPoints([(-20, 0)])
        .slot2D(20, 6, 0)
        .cutBlind(-15)
    )
    return (
        part.faces(">Z")
        .workplane()
        .pushPoints([(20, 0)])
        .slot2D(20, 6, 0)
        .cutBlind(-15)
    )


if __name__ == "__main__":
    out_path = Path(__file__).parent / "two_slots_part.step"
    cq.exporters.export(build_two_slots_part(), str(out_path))
    print(f"Wrote {out_path}")
