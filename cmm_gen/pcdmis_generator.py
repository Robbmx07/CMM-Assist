"""Assembles a PC-DMIS inspection routine from a fully validated
InspectionRoutine (features + mapped GD&T + approach vectors).

STATUS: this module is entirely gated on your answer to clarifying question
1 (PC-DMIS BASIC command syntax vs. neutral DMIS format) -- the two output
formats are different enough (command language + Cypress Enable BASIC
subroutines vs. ISO 6983-style DMIS statements) that building against the
wrong one means a rewrite, not a patch. Interfaces are defined below so the
rest of the pipeline can be developed and tested independently; the emitter
bodies raise NotImplementedError until that's settled.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from cmm_gen.models import ApproachVectorStatus, InspectionRoutine


class RoutineGenerationError(RuntimeError):
    """Raised when a routine has unmapped callouts or unresolved (rejected)
    approach vectors -- generation refuses to emit a program with silent
    gaps; the caller must resolve or explicitly acknowledge them first.
    """


def _validate_routine_is_complete(routine: InspectionRoutine) -> None:
    unmapped = [c for c in routine.callouts if c.mapped_feature_id is None]
    if unmapped:
        raise RoutineGenerationError(
            f"{len(unmapped)} GD&T callout(s) are not mapped to a feature: "
            f"{[c.id for c in unmapped]}"
        )
    rejected = [v for v in routine.approach_vectors if v.status != ApproachVectorStatus.VALID]
    if rejected:
        raise RoutineGenerationError(
            f"{len(rejected)} feature(s) have no valid approach vector: "
            f"{[(v.feature_id, v.status.value, v.rejection_reason) for v in rejected]}"
        )


def emit_alignment_block(routine: InspectionRoutine) -> str:
    """Emit the manual/DCC or iterative alignment block from routine.datums."""
    raise NotImplementedError(
        "emit_alignment_block: pending your answer on output format "
        "(PC-DMIS BASIC vs. DMIS) and datum hierarchy convention."
    )


def emit_clearance_moves(routine: InspectionRoutine) -> str:
    """Emit MOVE/POINT safety clearance moves between features."""
    raise NotImplementedError(
        "emit_clearance_moves: pending your answer on output format "
        "(PC-DMIS BASIC vs. DMIS)."
    )


def emit_feature_measurement(routine: InspectionRoutine) -> str:
    """Emit feature-measurement command blocks (one per CADFeature) with
    embedded DIM blocks carrying the mapped tolerances."""
    raise NotImplementedError(
        "emit_feature_measurement: pending your answer on output format "
        "(PC-DMIS BASIC vs. DMIS) and tolerance mapping conventions."
    )


def emit_conditional_subroutines(routine: InspectionRoutine) -> str:
    """Emit Cypress Enable BASIC subroutines for conditional logic (e.g.
    out-of-tolerance branching, SPC hooks)."""
    raise NotImplementedError(
        "emit_conditional_subroutines: pending your answer on output format."
    )


def generate_routine(routine: InspectionRoutine, output_path: Path) -> Path:
    """Top-level entry point: validated InspectionRoutine -> .PRG file on disk."""
    logger.info("Generating PC-DMIS routine for part: {}", routine.part_name)
    _validate_routine_is_complete(routine)

    sections = [
        emit_alignment_block(routine),
        emit_clearance_moves(routine),
        emit_feature_measurement(routine),
        emit_conditional_subroutines(routine),
    ]
    output_path.write_text("\n\n".join(sections), encoding="utf-8")
    logger.info("Wrote routine to {}", output_path)
    return output_path
