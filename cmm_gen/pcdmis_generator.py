"""Assembles a native PC-DMIS BASIC (.PRG) inspection routine from a fully
validated InspectionRoutine (features + mapped GD&T + approach vectors).

Target: current PC-DMIS BASIC command-language syntax (2023.x/2024.x line),
per your answers to the clarifying questions -- ASME Y14.5 datum/tolerance
conventions, native PC-DMIS BASIC output.

CONFIDENCE NOTE: the alignment (ALIGNMENT/LEVEL, ROTATE, ORIGIN), feature
(FEAT/..., THEO/ACTL/TARG), move (MOVE/POINT), and the common DIM types
(LOCATION, TRUEPOS, FLATNESS, PERPENDICULARITY, PARALLELISM, ANGULARITY,
CIRCULARITY, CYLINDRICITY, RUNOUT, STRAIGHTNESS) below follow well-documented
PC-DMIS command-language conventions. Less common DIM types (PROFILE,
TOTAL RUNOUT, CONCENTRICITY, SYMMETRY) and the exact PC-DMIS Basic COM object
names for reading dimension out-of-tolerance status in
`emit_conditional_subroutines` are marked inline with `$$ VERIFY` comments --
confirm those against your PC-DMIS installation/Basic object model reference
before running on the shop floor. This generator does not have access to a
live PC-DMIS instance to validate syntax against.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from cmm_gen.models import (
    ApproachVector,
    ApproachVectorStatus,
    CADFeature,
    DatumReference,
    FeatureType,
    GDTCallout,
    GDTCharacteristic,
    InspectionRoutine,
    Vector3,
)

# DIM types PC-DMIS documents with these exact command-language keywords.
_VERIFIED_DIM_KEYWORDS: dict[GDTCharacteristic, str] = {
    GDTCharacteristic.POSITION: "TRUEPOS",
    GDTCharacteristic.FLATNESS: "FLATNESS",
    GDTCharacteristic.PERPENDICULARITY: "PERPENDICULARITY",
    GDTCharacteristic.PARALLELISM: "PARALLELISM",
    GDTCharacteristic.ANGULARITY: "ANGULARITY",
    GDTCharacteristic.CIRCULARITY: "CIRCULARITY",
    GDTCharacteristic.CYLINDRICITY: "CYLINDRICITY",
    GDTCharacteristic.RUNOUT: "RUNOUT",
    GDTCharacteristic.STRAIGHTNESS: "STRAIGHTNESS",
    GDTCharacteristic.PLUS_MINUS: "LOCATION",
}
# DIM types whose exact PC-DMIS keyword/parameter layout should be confirmed
# against the shop's installed version before production use.
_UNVERIFIED_DIM_KEYWORDS: dict[GDTCharacteristic, str] = {
    GDTCharacteristic.PROFILE_SURFACE: "PROFILE",
    GDTCharacteristic.PROFILE_LINE: "PROFILE",
    GDTCharacteristic.CONCENTRICITY: "CONCENTRICITY",
    GDTCharacteristic.SYMMETRY: "SYMMETRY",
    GDTCharacteristic.TOTAL_RUNOUT: "TOTAL RUNOUT",
}

_DEFAULT_SAFE_Z_MM = 100.0
_HITS_PER_PLANE = 3
_HITS_PER_CIRCLE = 5
_LEVELS_PER_CYLINDER = 2
_HITS_PER_CYLINDER_LEVEL = 4
_HITS_PER_CONE = 5
# A cylindrical CADFeature whose depth/diameter ratio is below this is
# treated as a PC-DMIS CIRCLE feature (single-level hit pattern); above it,
# as a full CYLINDER feature (multi-level hit pattern). This is a
# measurement-strategy heuristic, not a geometry fact -- adjust per shop
# practice.
_CIRCLE_VS_CYLINDER_DEPTH_RATIO = 0.5


class RoutineGenerationError(RuntimeError):
    """Raised when a routine has unmapped callouts or unresolved (rejected)
    approach vectors -- generation refuses to emit a program with silent
    gaps; the caller must resolve or explicitly acknowledge them first.
    """


# --------------------------------------------------------------------------
# Vector / geometry helpers
# --------------------------------------------------------------------------


def _fmt(value: float) -> str:
    return f"{value:.4f}"


def _fmt_vec(v: Vector3) -> str:
    return f"{_fmt(v.x)},{_fmt(v.y)},{_fmt(v.z)}"


def _normalize(v: Vector3) -> Vector3:
    n = math.sqrt(v.x**2 + v.y**2 + v.z**2)
    if n < 1e-9:
        return Vector3(x=0, y=0, z=1)
    return Vector3(x=v.x / n, y=v.y / n, z=v.z / n)


def _orthonormal_basis(axis: Vector3) -> tuple[Vector3, Vector3]:
    """Return two unit vectors perpendicular to `axis` and to each other."""
    a = _normalize(axis)
    helper = Vector3(x=1, y=0, z=0) if abs(a.x) < 0.9 else Vector3(x=0, y=1, z=0)
    u = Vector3(
        x=a.y * helper.z - a.z * helper.y,
        y=a.z * helper.x - a.x * helper.z,
        z=a.x * helper.y - a.y * helper.x,
    )
    u = _normalize(u)
    v = Vector3(
        x=a.y * u.z - a.z * u.y,
        y=a.z * u.x - a.x * u.z,
        z=a.x * u.y - a.y * u.x,
    )
    return u, v


def _circle_hit_points(
    center: Vector3, axis: Vector3, radius: float, n_hits: int
) -> list[tuple[Vector3, Vector3]]:
    """Evenly spaced (point, outward_normal) pairs around a circle in the
    plane perpendicular to `axis`, centered at `center`."""
    u, v = _orthonormal_basis(axis)
    points: list[tuple[Vector3, Vector3]] = []
    for k in range(n_hits):
        theta = 2 * math.pi * k / n_hits
        offset = Vector3(
            x=u.x * math.cos(theta) + v.x * math.sin(theta),
            y=u.y * math.cos(theta) + v.y * math.sin(theta),
            z=u.z * math.cos(theta) + v.z * math.sin(theta),
        )
        point = Vector3(
            x=center.x + offset.x * radius,
            y=center.y + offset.y * radius,
            z=center.z + offset.z * radius,
        )
        # Outward normal for an internal bore/circle points inward toward
        # the axis (probe touches from inside pushing out), i.e. -offset.
        normal = Vector3(x=-offset.x, y=-offset.y, z=-offset.z)
        points.append((point, normal))
    return points


def _plane_hit_points(feature: CADFeature, n_hits: int) -> list[tuple[Vector3, Vector3]]:
    normal = _normalize(feature.nominal_vector)
    u, v = _orthonormal_basis(normal)
    # Spread hits over a modest radius derived from the bounding box when
    # available, else a conservative default -- real hit placement should
    # respect the actual face boundary, which the visualizer's "verify
    # before export" step is for.
    if feature.bounding_box is not None:
        bb = feature.bounding_box
        spread = max(
            abs(bb.max.x - bb.min.x), abs(bb.max.y - bb.min.y), abs(bb.max.z - bb.min.z)
        ) * 0.3
    else:
        spread = 5.0
    spread = max(spread, 2.0)

    points: list[tuple[Vector3, Vector3]] = []
    for k in range(n_hits):
        theta = 2 * math.pi * k / n_hits
        offset = Vector3(
            x=u.x * math.cos(theta) + v.x * math.sin(theta),
            y=u.y * math.cos(theta) + v.y * math.sin(theta),
            z=u.z * math.cos(theta) + v.z * math.sin(theta),
        )
        point = Vector3(
            x=feature.nominal_location.x + offset.x * spread,
            y=feature.nominal_location.y + offset.y * spread,
            z=feature.nominal_location.z + offset.z * spread,
        )
        points.append((point, normal))
    return points


def _feature_hit_points(feature: CADFeature) -> list[tuple[Vector3, Vector3]]:
    """Generate a hit pattern (point, outward-normal pairs) for a feature.
    This drives the THEO/ACTL/TARG lines under each FEAT/ definition.
    """
    if feature.type == FeatureType.PLANE:
        return _plane_hit_points(feature, _HITS_PER_PLANE)

    if feature.type == FeatureType.CIRCLE:
        radius = (feature.diameter or 0.0) / 2
        return _circle_hit_points(
            feature.nominal_location, feature.nominal_vector, radius, _HITS_PER_CIRCLE
        )

    if feature.type == FeatureType.CYLINDER:
        radius = (feature.diameter or 0.0) / 2
        depth = feature.depth or 0.0
        axis = _normalize(feature.nominal_vector)
        if feature.diameter and depth / feature.diameter < _CIRCLE_VS_CYLINDER_DEPTH_RATIO:
            return _circle_hit_points(feature.nominal_location, axis, radius, _HITS_PER_CIRCLE)
        points: list[tuple[Vector3, Vector3]] = []
        for level in range(_LEVELS_PER_CYLINDER):
            frac = level / max(_LEVELS_PER_CYLINDER - 1, 1) - 0.5
            level_center = Vector3(
                x=feature.nominal_location.x + axis.x * depth * frac,
                y=feature.nominal_location.y + axis.y * depth * frac,
                z=feature.nominal_location.z + axis.z * depth * frac,
            )
            points.extend(
                _circle_hit_points(level_center, axis, radius, _HITS_PER_CYLINDER_LEVEL)
            )
        return points

    if feature.type == FeatureType.CONE:
        radius = feature.radius or 0.0
        return _circle_hit_points(
            feature.nominal_location, feature.nominal_vector, radius, _HITS_PER_CONE
        )

    if feature.type == FeatureType.SLOT:
        # Two hits on each straight wall, offset +/- the slot width, plus
        # the two rounded ends -- a minimal but representative pattern.
        length_axis = _normalize(feature.nominal_vector)
        width = (feature.diameter or 0.0) / 2
        u, _ = _orthonormal_basis(length_axis)
        points = []
        for sign in (1, -1):
            wall_point = Vector3(
                x=feature.nominal_location.x + u.x * width * sign,
                y=feature.nominal_location.y + u.y * width * sign,
                z=feature.nominal_location.z + u.z * width * sign,
            )
            points.append((wall_point, Vector3(x=-u.x * sign, y=-u.y * sign, z=-u.z * sign)))
        return points

    logger.warning("No hit-point strategy for feature type {}; skipping hits.", feature.type)
    return []


def _pcdmis_feature_type(feature: CADFeature) -> str:
    if feature.type == FeatureType.CYLINDER:
        depth = feature.depth or 0.0
        if feature.diameter and depth / feature.diameter < _CIRCLE_VS_CYLINDER_DEPTH_RATIO:
            return "CIRCLE"
        return "CYLINDER"
    return str(feature.type.value)


# --------------------------------------------------------------------------
# Program header / alignment
# --------------------------------------------------------------------------


def emit_header(routine: InspectionRoutine) -> str:
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"$$ Part: {routine.part_name}",
        f"$$ Generated by CMM-Gen on {generated}",
        f"$$ Probe: {routine.probe_config.name} ({routine.probe_config.head_type})",
        "UNITS/MM,ANGLES=DEG",
        "MODE/DCC",
    ]
    return "\n".join(lines)


def emit_alignment_block(
    routine: InspectionRoutine, mode: str = "manual_dcc", iterations: int = 3
) -> str:
    """Emit the alignment block from routine.datums, using ASME Y14.5
    primary/secondary/tertiary datum precedence (A|B|C order).

    `mode` is "manual_dcc" for a single-pass LEVEL/ROTATE/ORIGIN sequence,
    or "iterative" to wrap the same sequence in an ALIGNMENT/ITERATE block
    (re-run until convergence -- useful when the primary/secondary datum
    features interact, e.g. on thin or flexible parts).
    """
    datums = sorted(routine.datums, key=lambda d: d.precedence)
    if len(datums) < 3:
        raise RoutineGenerationError(
            f"ASME Y14.5 alignment requires primary/secondary/tertiary datums; "
            f"got {len(datums)}: {[d.label for d in datums]}"
        )
    primary, secondary, tertiary = datums[0], datums[1], datums[2]

    body = [
        "ALIGNMENT/START,RECALL:NO,LIST=NO",
        f"$$ Datum {primary.label} (primary) -- level",
        f"ALIGNMENT/LEVEL,ZPLUS,{primary.feature_id}",
        f"$$ Datum {secondary.label} (secondary) -- rotate",
        f"ALIGNMENT/ROTATE,XPLUS,TO,{secondary.feature_id},ABOUT,ZPLUS",
        f"$$ Datum {tertiary.label} (tertiary) -- origin",
        f"ALIGNMENT/ORIGIN,{tertiary.feature_id},X,Y,Z",
        "ALIGNMENT/END",
    ]

    if mode == "manual_dcc":
        return "\n".join(body)
    if mode == "iterative":
        return "\n".join(
            [f"ALIGNMENT/ITERATE,BEGIN,{iterations},0.0100", *body, "ALIGNMENT/ITERATE,END"]
        )
    raise RoutineGenerationError(f"Unknown alignment mode: {mode}")


# --------------------------------------------------------------------------
# Clearance moves
# --------------------------------------------------------------------------


def emit_clearance_move(point: Vector3, direction: Vector3) -> str:
    return f"MOVE/POINT,{_fmt_vec(point)},{_fmt_vec(_normalize(direction))}"


def emit_safe_start_move(routine: InspectionRoutine, safe_z: float = _DEFAULT_SAFE_Z_MM) -> str:
    return emit_clearance_move(
        Vector3(x=0, y=0, z=safe_z), Vector3(x=0, y=0, z=-1)
    )


# --------------------------------------------------------------------------
# Feature + DIM emission
# --------------------------------------------------------------------------


def _emit_feature_definition(feature: CADFeature) -> str:
    pcdmis_type = _pcdmis_feature_type(feature)
    hits = _feature_hit_points(feature)
    lines = [f"F({feature.id})=FEAT/{pcdmis_type},CARTESIAN"]
    for point, normal in hits:
        theo_actl = f"{_fmt_vec(point)},{_fmt_vec(_normalize(normal))}"
        lines.append(f"THEO/{theo_actl}")
        lines.append(f"ACTL/{theo_actl}")
        lines.append(f"TARG/{theo_actl}")
    if feature.diameter is not None and pcdmis_type in ("CIRCLE", "CYLINDER", "SLOT"):
        lines.append(f"DIAM/THEO,{_fmt(feature.diameter)}")
    return "\n".join(lines)


def _dim_keyword(characteristic: GDTCharacteristic) -> tuple[str, bool]:
    """Return (keyword, verified)."""
    if characteristic in _VERIFIED_DIM_KEYWORDS:
        return _VERIFIED_DIM_KEYWORDS[characteristic], True
    return _UNVERIFIED_DIM_KEYWORDS[characteristic], False


def _emit_dim_block(callout: GDTCallout, dim_index: int) -> str:
    keyword, verified = _dim_keyword(callout.characteristic)
    dim_name = f"{keyword.replace(' ', '')[:8]}{dim_index}"
    lines = []
    if not verified:
        lines.append(
            f"$$ VERIFY: exact DIM syntax for {callout.characteristic.value} "
            f"(PC-DMIS keyword '{keyword}') against your installation."
        )

    header = f"DIM {dim_name}={keyword} OF {callout.mapped_feature_id} UNITS=MM,$"
    lines.append(header)
    lines.append("    TEXT=NO,$")

    if callout.datums:

        def _datum_label(d: DatumReference) -> str:
            if d.material_condition.value == "RFS":
                return d.label
            return f"{d.label}({d.material_condition.value})"

        datum_str = "|".join(_datum_label(d) for d in callout.datums)
        lines.append(f"    DATUMS={datum_str},$")

    tol = _fmt(callout.tolerance_value)
    if callout.characteristic == GDTCharacteristic.POSITION:
        zone = "DIAMETER" if callout.diameter_zone else "WIDTH"
        lines.append(f"    ZONETYPE={zone},$")
        lines.append("    AX     NOMINAL         TOL        MEAS         DEV      OUTTOL")
        lines.append(f"    D      0.0000     {tol}")
    elif characteristic_is_form_or_orientation(callout.characteristic):
        lines.append("    AX        TOL        MEAS         DEV      OUTTOL")
        lines.append(f"    T      {tol}")
    else:  # PLUS_MINUS / LOCATION-style
        lines.append("    AX     NOMINAL      +TOL       -TOL        MEAS         DEV     OUTTOL")
        lines.append(f"    D      {tol}      {tol}      {tol}")

    lines.append(f"END OF DIM {dim_name}")
    return "\n".join(lines)


def characteristic_is_form_or_orientation(characteristic: GDTCharacteristic) -> bool:
    return characteristic in {
        GDTCharacteristic.FLATNESS,
        GDTCharacteristic.PERPENDICULARITY,
        GDTCharacteristic.PARALLELISM,
        GDTCharacteristic.ANGULARITY,
        GDTCharacteristic.CIRCULARITY,
        GDTCharacteristic.CYLINDRICITY,
        GDTCharacteristic.RUNOUT,
        GDTCharacteristic.TOTAL_RUNOUT,
        GDTCharacteristic.STRAIGHTNESS,
        GDTCharacteristic.CONCENTRICITY,
        GDTCharacteristic.SYMMETRY,
        GDTCharacteristic.PROFILE_SURFACE,
        GDTCharacteristic.PROFILE_LINE,
    }


def emit_feature_measurement(routine: InspectionRoutine) -> str:
    """Emit, per feature: a safety clearance move to its clearance point,
    the FEAT/ definition with generated hit points, any mapped DIM
    tolerance block(s), and a retract move back to clearance."""
    vectors_by_feature: dict[str, ApproachVector] = {
        av.feature_id: av for av in routine.approach_vectors
    }
    callouts_by_feature: dict[str, list[GDTCallout]] = {}
    for c in routine.callouts:
        if c.mapped_feature_id:
            callouts_by_feature.setdefault(c.mapped_feature_id, []).append(c)

    sections: list[str] = []
    dim_index = 1
    for feature in routine.features:
        av = vectors_by_feature.get(feature.id)
        if av is None:
            logger.warning(
                "Feature {} has no approach vector in this routine; skipping measurement.",
                feature.id,
            )
            continue

        block = [
            f"$$ --- {feature.id} ({feature.type.value}) ---",
            emit_clearance_move(av.clearance_point, av.approach_vector),
            _emit_feature_definition(feature),
        ]
        for callout in callouts_by_feature.get(feature.id, []):
            block.append(_emit_dim_block(callout, dim_index))
            dim_index += 1
        block.append(emit_clearance_move(av.retract_point, Vector3(x=0, y=0, z=1)))
        sections.append("\n".join(block))

    return "\n\n".join(sections)


# --------------------------------------------------------------------------
# Cypress Enable BASIC conditional logic
# --------------------------------------------------------------------------


def emit_conditional_subroutines(routine: InspectionRoutine) -> str:
    """Emit a Cypress Enable BASIC subroutine that checks every mapped DIM
    for out-of-tolerance status and reports pass/fail.

    $$ VERIFY: `Dimension(name).OutTol` below is the commonly documented
    PC-DMIS Basic COM property for reading a dimension's out-of-tolerance
    flag by name; confirm the exact property/method against your PC-DMIS
    Basic object model reference (it can differ slightly by version).
    """
    dim_count = sum(1 for c in routine.callouts if c.mapped_feature_id)
    if dim_count == 0:
        return "$$ No mapped GD&T callouts -- no conditional subroutine generated."

    lines = [
        "'$$ --- Cypress Enable BASIC: tolerance-check subroutine ---",
        "'$$ VERIFY the Dimension(...).OutTol property name against your",
        "'$$ PC-DMIS Basic object model reference before production use.",
        "DIM partRejected AS INTEGER",
        "partRejected = 0",
        "",
        "SUB CheckAllDimensions()",
        "    DIM d AS OBJECT",
        "    FOR EACH d IN ThisDocument.Dimensions",
        "        IF d.OutTol = TRUE THEN",
        "            partRejected = 1",
        '            PRINT "OUT OF TOLERANCE: " & d.Name',
        "        ENDIF",
        "    NEXT d",
        "END SUB",
        "",
        "CALL CheckAllDimensions()",
        "",
        "IF partRejected = 1 THEN",
        '    PRINT "PART STATUS: REJECTED"',
        "ELSE",
        '    PRINT "PART STATUS: ACCEPTED"',
        "ENDIF",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Validation + top-level entry point
# --------------------------------------------------------------------------


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


def generate_routine(routine: InspectionRoutine, output_path: Path) -> Path:
    """Top-level entry point: validated InspectionRoutine -> .PRG file on disk."""
    logger.info("Generating PC-DMIS routine for part: {}", routine.part_name)
    _validate_routine_is_complete(routine)

    sections = [
        emit_header(routine),
        emit_safe_start_move(routine),
        emit_alignment_block(routine),
        emit_feature_measurement(routine),
        emit_conditional_subroutines(routine),
    ]
    output_path.write_text("\n\n".join(sections), encoding="utf-8")
    logger.info("Wrote routine to {}", output_path)
    return output_path
