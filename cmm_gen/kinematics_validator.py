"""Collision and accessibility engine.

Given a CADFeature and a candidate approach direction, computes a full
ApproachVector (contact point, clearance point, retract point) and validates
it against:
  - the probe/head kinematic envelope (A/B angle limits, indexable-head
    discrete angle sets vs. continuous 5-axis),
  - the machine's travel limits,
  - fixture/part self-collision (clearance point must lie outside the part's
    own bounding box, and the approach path must not cross a declared
    fixture keep-out box).

Any vector that fails a check is returned with status != VALID and a
rejection_reason -- it is never silently dropped, so the generator and the
visualizer can show exactly why a feature was flagged unreachable.

Vector convention (matches ApproachVector in models.py): `approach_vector`
points FROM the clearance point TOWARD the surface, i.e. the direction the
probe travels while approaching. The clearance point is therefore
`contact_point - approach_vector * standoff`, not `+`.

Probe hardware: shop probe/module names (HH-A-T5, HA-TM-31, HP-TBe,
HT-TM-MF) are defined in config/probe_library.yaml. Their exact tip/stylus/
index-angle specs are PLACEHOLDERS pending the real datasheet values --
`load_probe_config` logs a warning for any entry still marked
`verified: false` in that file.

Collision model: this is a simplified proxy (swept-path vs. axis-aligned
keep-out boxes, clearance-point-vs-part-bbox for blind spots), not a full
solid-vs-solid sweep against the actual STEP geometry. It catches the classes
of error the spec calls out explicitly (blind spots requiring fixture
clearance, head angles beyond hardware limits) but is not a substitute for a
real swept-volume collision check against the part mesh -- that upgrade path
is noted inline in check_collision.
"""

from __future__ import annotations

import math
from pathlib import Path

import yaml
from loguru import logger

from cmm_gen.models import (
    ApproachVector,
    ApproachVectorStatus,
    BoundingBox,
    CADFeature,
    FeatureType,
    MachineEnvelope,
    ProbeConfig,
    ProbeTip,
    Vector3,
)

DEFAULT_PROBE_LIBRARY_PATH = Path(__file__).parent.parent / "config" / "probe_library.yaml"
DEFAULT_MACHINE_ENVELOPE_PATH = (
    Path(__file__).parent.parent / "config" / "machine_envelope.yaml"
)

# How far outside the surface the clearance/retract point sits, in addition
# to the stylus/extension length, so the ball doesn't clip the part on
# approach.
_STANDOFF_MARGIN_MM = 5.0

# Angular slop allowed when snapping a desired approach vector onto an
# indexable head's discrete A/B grid before rejecting it as unreachable.
_INDEX_SNAP_TOLERANCE_DEG = 1.0


class KinematicsValidationError(RuntimeError):
    """Raised for configuration errors (e.g. malformed probe/envelope), not
    for a rejected-but-well-formed approach vector -- those are represented
    as ApproachVector(status=REJECTED_*), not exceptions."""


# --------------------------------------------------------------------------
# Vector helpers
# --------------------------------------------------------------------------


def _sub(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(x=a.x - b.x, y=a.y - b.y, z=a.z - b.z)


def _add_scaled(a: Vector3, b: Vector3, scale: float) -> Vector3:
    return Vector3(x=a.x + b.x * scale, y=a.y + b.y * scale, z=a.z + b.z * scale)


def _negate(v: Vector3) -> Vector3:
    return Vector3(x=-v.x, y=-v.y, z=-v.z)


def _norm(v: Vector3) -> float:
    return math.sqrt(v.x**2 + v.y**2 + v.z**2)


def _normalize(v: Vector3) -> Vector3:
    n = _norm(v)
    if n < 1e-9:
        return Vector3(x=0.0, y=0.0, z=0.0)
    return Vector3(x=v.x / n, y=v.y / n, z=v.z / n)


def _cross(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(
        x=a.y * b.z - a.z * b.y,
        y=a.z * b.x - a.x * b.z,
        z=a.x * b.y - a.y * b.x,
    )


def _dot(a: Vector3, b: Vector3) -> float:
    return a.x * b.x + a.y * b.y + a.z * b.z


# --------------------------------------------------------------------------
# Probe library
# --------------------------------------------------------------------------


def load_probe_config(name: str, library_path: Path = DEFAULT_PROBE_LIBRARY_PATH) -> ProbeConfig:
    """Load a named probe configuration from the shop's probe library YAML."""
    if not library_path.exists():
        raise KinematicsValidationError(f"Probe library not found: {library_path}")

    data = yaml.safe_load(library_path.read_text())
    probes = data.get("probes", {})
    if name not in probes:
        raise KinematicsValidationError(
            f"Probe '{name}' not found in {library_path}. Available: {sorted(probes)}"
        )

    entry = probes[name]
    if not entry.get("verified", False):
        logger.warning(
            "Probe config '{}' is loaded from a PLACEHOLDER spec (verified: false) "
            "-- confirm tip/stylus/angle values against the datasheet before "
            "trusting reachability results.",
            name,
        )

    tip_data = entry["tip"]
    return ProbeConfig(
        name=name,
        head_type=entry["head_type"],
        tip=ProbeTip(
            diameter_mm=tip_data["diameter_mm"],
            stylus_length_mm=tip_data["stylus_length_mm"],
            material=tip_data.get("material", "ruby"),
        ),
        extension_length_mm=entry.get("extension_length_mm", 0.0),
        index_increment_deg=entry.get("index_increment_deg", 7.5),
        a_angle_range_deg=tuple(entry.get("a_angle_range_deg", (-90.0, 105.0))),
        b_angle_range_deg=tuple(entry.get("b_angle_range_deg", (-180.0, 180.0))),
        notes=entry.get("notes"),
    )


# --------------------------------------------------------------------------
# Machine envelope library
# --------------------------------------------------------------------------


def load_machine_envelope(
    name: str = "default", library_path: Path = DEFAULT_MACHINE_ENVELOPE_PATH
) -> MachineEnvelope:
    """Load a named machine travel envelope from the shop's config YAML."""
    if not library_path.exists():
        raise KinematicsValidationError(f"Machine envelope config not found: {library_path}")

    data = yaml.safe_load(library_path.read_text())
    machines = data.get("machines", {})
    if name not in machines:
        raise KinematicsValidationError(
            f"Machine '{name}' not found in {library_path}. Available: {sorted(machines)}"
        )

    entry = machines[name]
    if not entry.get("verified", False):
        logger.warning(
            "Machine envelope '{}' is loaded from a PLACEHOLDER spec (verified: false) "
            "-- confirm travel limits and fixture keep-out boxes before trusting "
            "collision results.",
            name,
        )

    keepout_boxes = [
        BoundingBox(
            min=Vector3(**box["min"]),
            max=Vector3(**box["max"]),
        )
        for box in entry.get("fixture_keepout_boxes", [])
    ]
    return MachineEnvelope(
        x_travel_mm=tuple(entry["x_travel_mm"]),
        y_travel_mm=tuple(entry["y_travel_mm"]),
        z_travel_mm=tuple(entry["z_travel_mm"]),
        fixture_keepout_boxes=keepout_boxes,
    )


# --------------------------------------------------------------------------
# Candidate approach vectors
# --------------------------------------------------------------------------


def candidate_approach_vectors(feature: CADFeature) -> list[Vector3]:
    """Generate candidate unit approach directions for a feature, ordered
    from most to least likely to be feasible.

    - PLANE: the single direction opposite the surface normal (probe
      travels against the outward normal to touch the surface; cad_parser
      guarantees this normal is topologically outward-corrected).
    - CYLINDER / CONE / CIRCLE: both signs along the axis/edge-normal --
      for cylinders/cones the bore entry direction isn't always recoverable
      from geometry alone, and for CIRCLE (an edge, not a face) the normal
      sign is not guaranteed to be outward the way a face normal is -- in
      both cases whichever direction is actually open/unobstructed is left
      to pass validation.
    - SLOT: no surface normal is available from the extracted geometry, so
      this falls back to vertical entry (-Z, the common case for a milled
      slot) plus the two horizontal directions perpendicular to the slot's
      long axis.
    """
    unit = _normalize(feature.nominal_vector)
    if _norm(unit) < 1e-9:
        logger.warning(
            "Feature {} has a degenerate nominal_vector; no candidate "
            "approach vectors can be generated.",
            feature.id,
        )
        return []

    if feature.type == FeatureType.PLANE:
        return [_negate(unit)]

    if feature.type in (FeatureType.CYLINDER, FeatureType.CONE, FeatureType.CIRCLE):
        return [_negate(unit), unit]

    if feature.type == FeatureType.SLOT:
        candidates = [Vector3(x=0.0, y=0.0, z=-1.0)]
        global_up = Vector3(x=0.0, y=0.0, z=1.0)
        perp = _cross(unit, global_up)
        if _norm(perp) > 1e-6:
            perp_unit = _normalize(perp)
            candidates.append(perp_unit)
            candidates.append(_negate(perp_unit))
        return candidates

    # SPHERE / LINE / POINT fallback: approach opposite the given vector.
    return [_negate(unit)]


# --------------------------------------------------------------------------
# Head-angle feasibility
# --------------------------------------------------------------------------


def _vector_to_head_angles(vector: Vector3) -> tuple[float, float]:
    """Convert a unit approach-direction vector into (A, B) head angles,
    where A is measured from straight-down (0,0,-1) and B is the azimuth
    rotation about Z -- the standard indexable-head (e.g. PH10) convention.
    """
    v = _normalize(vector)
    cos_a = max(-1.0, min(1.0, -v.z))
    a_deg = math.degrees(math.acos(cos_a))
    b_deg = math.degrees(math.atan2(v.y, v.x))
    return a_deg, b_deg


def _head_angles_to_vector(a_deg: float, b_deg: float) -> Vector3:
    a = math.radians(a_deg)
    b = math.radians(b_deg)
    return Vector3(
        x=math.sin(a) * math.cos(b),
        y=math.sin(a) * math.sin(b),
        z=-math.cos(a),
    )


def _angle_within_range(angle_deg: float, angle_range: tuple[float, float]) -> bool:
    lo, hi = angle_range
    return lo - 1e-6 <= angle_deg <= hi + 1e-6


def _wrap_deg(angle_deg: float) -> float:
    return ((angle_deg + 180.0) % 360.0) - 180.0


def check_head_angle_feasible(vector: Vector3, probe: ProbeConfig) -> tuple[bool, str | None]:
    """Return (True, None) if the probe head can orient to reach along
    `vector`, else (False, reason)."""
    a_deg, b_deg = _vector_to_head_angles(vector)

    if probe.head_type == "fixed":
        fixed_vector = _head_angles_to_vector(probe.a_angle_deg, probe.b_angle_deg)
        angle_between_deg = math.degrees(
            math.acos(max(-1.0, min(1.0, _dot(_normalize(vector), fixed_vector))))
        )
        if angle_between_deg <= _INDEX_SNAP_TOLERANCE_DEG:
            return True, None
        return False, (
            f"Fixed head '{probe.name}' is oriented at A={probe.a_angle_deg:.1f}, "
            f"B={probe.b_angle_deg:.1f} and cannot reorient; requested direction "
            f"needs A={a_deg:.1f}, B={b_deg:.1f} ({angle_between_deg:.1f} deg off)."
        )

    if not _angle_within_range(a_deg, probe.a_angle_range_deg):
        return False, (
            f"Required A angle {a_deg:.1f} deg is outside probe '{probe.name}' "
            f"range {probe.a_angle_range_deg}."
        )
    if not _angle_within_range(_wrap_deg(b_deg), probe.b_angle_range_deg):
        return False, (
            f"Required B angle {b_deg:.1f} deg is outside probe '{probe.name}' "
            f"range {probe.b_angle_range_deg}."
        )

    if probe.head_type == "continuous_5axis":
        return True, None

    if probe.head_type == "indexable":
        step = probe.index_increment_deg
        snapped_a = round(a_deg / step) * step
        snapped_b = round(b_deg / step) * step
        snapped_vector = _head_angles_to_vector(snapped_a, snapped_b)
        deviation_deg = math.degrees(
            math.acos(max(-1.0, min(1.0, _dot(_normalize(vector), snapped_vector))))
        )
        if deviation_deg <= _INDEX_SNAP_TOLERANCE_DEG:
            return True, None
        return False, (
            f"Required direction (A={a_deg:.1f}, B={b_deg:.1f}) is {deviation_deg:.1f} deg "
            f"away from the nearest reachable index position on probe '{probe.name}' "
            f"({step} deg steps; nearest is A={snapped_a:.1f}, B={snapped_b:.1f})."
        )

    raise KinematicsValidationError(f"Unknown head_type '{probe.head_type}' on probe {probe.name}")


# --------------------------------------------------------------------------
# Travel limits
# --------------------------------------------------------------------------


def check_travel_limits(point: Vector3, envelope: MachineEnvelope) -> tuple[bool, str | None]:
    x_ok = envelope.x_travel_mm[0] <= point.x <= envelope.x_travel_mm[1]
    y_ok = envelope.y_travel_mm[0] <= point.y <= envelope.y_travel_mm[1]
    z_ok = envelope.z_travel_mm[0] <= point.z <= envelope.z_travel_mm[1]
    if x_ok and y_ok and z_ok:
        return True, None
    return False, f"Point {point.as_tuple()} exceeds machine travel envelope"


# --------------------------------------------------------------------------
# Collision / clearance
# --------------------------------------------------------------------------


def _point_in_box(point: Vector3, box: BoundingBox, margin: float = 0.0) -> bool:
    return (
        box.min.x - margin <= point.x <= box.max.x + margin
        and box.min.y - margin <= point.y <= box.max.y + margin
        and box.min.z - margin <= point.z <= box.max.z + margin
    )


def _segment_intersects_box(p0: Vector3, p1: Vector3, box: BoundingBox) -> bool:
    """Slab-method segment/AABB intersection test."""
    t_min, t_max = 0.0, 1.0
    d = _sub(p1, p0)
    for axis in ("x", "y", "z"):
        p0_a, d_a = getattr(p0, axis), getattr(d, axis)
        lo, hi = getattr(box.min, axis), getattr(box.max, axis)
        if abs(d_a) < 1e-9:
            if p0_a < lo or p0_a > hi:
                return False
            continue
        t1 = (lo - p0_a) / d_a
        t2 = (hi - p0_a) / d_a
        t1, t2 = min(t1, t2), max(t1, t2)
        t_min = max(t_min, t1)
        t_max = min(t_max, t2)
        if t_min > t_max:
            return False
    return True


def compute_standoff_distance(probe: ProbeConfig) -> float:
    return probe.tip.stylus_length_mm + probe.extension_length_mm + _STANDOFF_MARGIN_MM


def check_collision(
    feature: CADFeature,
    vector: Vector3,
    probe: ProbeConfig,
    envelope: MachineEnvelope,
    part_bbox: BoundingBox | None = None,
) -> tuple[bool, str | None]:
    """Check the approach path from clearance to contact against fixture
    keep-out boxes, and flag blind spots where the clearance point would
    have to sit inside the part's own bounding box (no room to retract
    without passing back through material -- the "blind spot underneath the
    part" case called out in the spec).

    NOTE: this is a bounding-box-based proxy, not a full swept-solid vs.
    solid collision check against the actual STEP geometry. Upgrading it to
    a real check means sweeping the probe tip + stylus + extension as a
    capsule along the approach path and testing against the imported
    cadquery.Shape faces directly (via BRepExtrema or a discretized mesh
    distance query) instead of against `part_bbox` -- left for a follow-up
    once the bbox-based version is validated against real parts.
    """
    contact_point = feature.nominal_location
    standoff = compute_standoff_distance(probe)
    clearance_point = _add_scaled(contact_point, vector, -standoff)

    if part_bbox is not None and _point_in_box(clearance_point, part_bbox, margin=-1e-6):
        return False, (
            f"Clearance point {clearance_point.as_tuple()} for feature {feature.id} lies "
            "inside the part's own bounding box: this approach direction points into "
            "solid material (blind spot) rather than away from the part -- check for a "
            "fixture-clearance or wrist-angle issue on the opposite face."
        )

    for i, box in enumerate(envelope.fixture_keepout_boxes):
        if _segment_intersects_box(clearance_point, contact_point, box):
            return False, (
                f"Approach path for feature {feature.id} from {clearance_point.as_tuple()} "
                f"to {contact_point.as_tuple()} crosses fixture keep-out box #{i}."
            )

    return True, None


# --------------------------------------------------------------------------
# Top-level entry point
# --------------------------------------------------------------------------


def validate_feature(
    feature: CADFeature,
    probe: ProbeConfig,
    envelope: MachineEnvelope,
    part_bbox: BoundingBox | None = None,
) -> ApproachVector:
    """Try each candidate approach vector for a feature in order and return
    the first that passes all checks, or the best-documented rejection if
    none pass."""
    candidates = candidate_approach_vectors(feature)
    last_rejection: ApproachVector | None = None

    def _reject(
        contact_point: Vector3,
        vector: Vector3,
        clearance_point: Vector3,
        status: ApproachVectorStatus,
        reason: str,
    ) -> ApproachVector:
        return ApproachVector(
            feature_id=feature.id,
            contact_point=contact_point,
            approach_vector=vector,
            clearance_point=clearance_point,
            retract_point=clearance_point,
            status=status,
            rejection_reason=reason,
        )

    for vector in candidates:
        contact_point = feature.nominal_location
        standoff = compute_standoff_distance(probe)
        clearance_point = _add_scaled(contact_point, vector, -standoff)

        ok, reason = check_travel_limits(clearance_point, envelope)
        if not ok:
            last_rejection = _reject(
                contact_point,
                vector,
                clearance_point,
                ApproachVectorStatus.REJECTED_TRAVEL_LIMIT,
                reason or "",
            )
            continue

        ok, reason = check_head_angle_feasible(vector, probe)
        if not ok:
            last_rejection = _reject(
                contact_point,
                vector,
                clearance_point,
                ApproachVectorStatus.REJECTED_HEAD_ANGLE,
                reason or "",
            )
            continue

        ok, reason = check_collision(feature, vector, probe, envelope, part_bbox)
        if not ok:
            status = (
                ApproachVectorStatus.REJECTED_BLIND_SPOT
                if "blind spot" in (reason or "")
                else ApproachVectorStatus.REJECTED_COLLISION
            )
            last_rejection = _reject(contact_point, vector, clearance_point, status, reason or "")
            continue

        return ApproachVector(
            feature_id=feature.id,
            contact_point=contact_point,
            approach_vector=vector,
            clearance_point=clearance_point,
            retract_point=clearance_point,
            status=ApproachVectorStatus.VALID,
        )

    logger.warning(
        "Feature {} has no feasible approach vector ({} candidates tried); last reason: {}",
        feature.id,
        len(candidates),
        last_rejection.rejection_reason if last_rejection else "no candidates generated",
    )
    if last_rejection is not None:
        return last_rejection
    return ApproachVector(
        feature_id=feature.id,
        contact_point=feature.nominal_location,
        approach_vector=feature.nominal_vector,
        clearance_point=feature.nominal_location,
        retract_point=feature.nominal_location,
        status=ApproachVectorStatus.REJECTED_BLIND_SPOT,
        rejection_reason="No candidate approach vectors could be generated for this feature",
    )
