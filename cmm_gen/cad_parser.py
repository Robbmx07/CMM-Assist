"""Parses .STEP files into the CADFeature schema defined in models.py.

Backend: CadQuery (OCP/OpenCascade bindings). Swap to pythonocc-core by
reimplementing `_load_shape` and the per-surface-type extractors below if the
shop standardizes on that stack instead -- everything downstream only depends
on the CADFeature list, not on the CAD kernel used to produce it.

Feature-ID scheme: sequential per type in face/edge traversal order, e.g.
PLN_001, CYL_002, CON_003, SLT_004, CIR_005. IDs are stable for a given STEP
file + traversal order but are NOT guaranteed stable across re-exports of the
same CAD model from different authoring tools (STEP entity ordering is not
part of the standard) -- gdt_extractor's feature matching must key off
geometry (nominal location/vector/diameter), not off these IDs alone.

Feature-type notes:
  - A CYLINDER CADFeature is emitted once per full (>=350deg swept) cylindrical
    face -- this covers both through-holes and blind bores faithfully as CAD
    geometry. Deciding whether PC-DMIS should measure it as a CIRCLE feature
    (single Z-level, few hits) or a CYLINDER feature (multiple Z-levels) is a
    measurement-strategy decision, not a geometry fact, so that choice is left
    to pcdmis_generator (it can use `depth / diameter` as a heuristic).
  - A CIRCLE CADFeature is only emitted for circular EDGES that are not the
    rim of an already-extracted CYLINDER face -- e.g. a scribed/stamped
    circle on a flat or sheet-metal part with no associated bore.
  - A SLOT CADFeature is reconstructed by pairing the two half-cylindrical
    "end cap" faces of an obround pocket/slot (partial cylindrical faces with
    ~180deg angular sweep, matching radius, parallel axes). This is a
    heuristic: unusual slot geometry (non-obround, angled) may not pair up
    and will instead surface as two unmatched partial-cylinder faces, logged
    as a warning rather than silently dropped or misclassified.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from cmm_gen.models import BoundingBox, CADFeature, FeatureType, Vector3

# A cylindrical face is treated as "full" (a bore) above this angular sweep,
# and as a candidate slot end-cap between the min/max below.
_FULL_SWEEP_RAD = math.radians(350)
_SLOT_ENDCAP_SWEEP_MIN_RAD = math.radians(150)
_SLOT_ENDCAP_SWEEP_MAX_RAD = math.radians(210)

# Tolerances for matching/pairing/deduping geometry.
_LENGTH_TOL_MM = 1e-3
_ANGLE_TOL_RAD = math.radians(1.0)

_TYPE_PREFIX = {
    FeatureType.PLANE: "PLN",
    FeatureType.CYLINDER: "CYL",
    FeatureType.CONE: "CON",
    FeatureType.SLOT: "SLT",
    FeatureType.CIRCLE: "CIR",
    FeatureType.SPHERE: "SPH",
    FeatureType.LINE: "LIN",
    FeatureType.POINT: "PNT",
}


class CADParseError(RuntimeError):
    """Raised when a STEP file cannot be loaded or contains no recognizable features."""


class _IdAllocator:
    def __init__(self) -> None:
        self._counters: dict[FeatureType, int] = {}

    def next_id(self, feature_type: FeatureType) -> str:
        n = self._counters.get(feature_type, 0) + 1
        self._counters[feature_type] = n
        return f"{_TYPE_PREFIX[feature_type]}_{n:03d}"


def load_step_file(path: Path) -> cadquery.Shape:  # type: ignore[name-defined]  # noqa: F821
    """Load a STEP file and return the root CadQuery Shape.

    Raises CADParseError if the file is missing, unreadable, or empty.
    """
    import cadquery as cq

    if not path.exists():
        raise CADParseError(f"STEP file not found: {path}")
    try:
        result = cq.importers.importStep(str(path))
    except Exception as exc:  # noqa: BLE001 - re-raised with context
        raise CADParseError(f"Failed to parse STEP file {path}: {exc}") from exc
    if result.val() is None:
        raise CADParseError(f"STEP file {path} parsed but contains no solids")
    return result.val()


def compute_bounding_box(shape: cadquery.Shape) -> BoundingBox:  # type: ignore[name-defined]  # noqa: F821
    bb = shape.BoundingBox()
    return BoundingBox(
        min=Vector3(x=bb.xmin, y=bb.ymin, z=bb.zmin),
        max=Vector3(x=bb.xmax, y=bb.ymax, z=bb.zmax),
    )


def _face_bbox(face: cadquery.Face) -> BoundingBox:  # type: ignore[name-defined]  # noqa: F821
    bb = face.BoundingBox()
    return BoundingBox(
        min=Vector3(x=bb.xmin, y=bb.ymin, z=bb.zmin),
        max=Vector3(x=bb.xmax, y=bb.ymax, z=bb.zmax),
    )


@dataclass
class _PartialCylinder:
    face: cadquery.Face  # type: ignore[name-defined]  # noqa: F821
    radius: float
    axis_location: Vector3
    axis_direction: Vector3
    v_min: float
    v_max: float


def _axis_point_at(axis_location: Vector3, axis_direction: Vector3, v: float) -> Vector3:
    return Vector3(
        x=axis_location.x + axis_direction.x * v,
        y=axis_location.y + axis_direction.y * v,
        z=axis_location.z + axis_direction.z * v,
    )


def _directions_parallel(a: Vector3, b: Vector3, tol_rad: float = _ANGLE_TOL_RAD) -> bool:
    dot = a.x * b.x + a.y * b.y + a.z * b.z
    dot = max(-1.0, min(1.0, dot))
    angle = math.acos(abs(dot))
    return angle <= tol_rad


def _distance(a: Vector3, b: Vector3) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def _classify_faces(
    shape: cadquery.Shape,  # type: ignore[name-defined]  # noqa: F821
    ids: _IdAllocator,
) -> tuple[list[CADFeature], list[_PartialCylinder], set[tuple[float, float, float, float]]]:
    """Single pass over all faces. Returns (features, slot_candidates,
    covered_bore_keys) where covered_bore_keys lets circle-edge extraction
    skip edges that are just the rim of an already-extracted bore.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_SurfaceType
    from OCP.TopAbs import TopAbs_Orientation

    features: list[CADFeature] = []
    slot_candidates: list[_PartialCylinder] = []
    covered_bore_keys: set[tuple[float, float, float, float]] = set()

    for face in shape.Faces():
        adaptor = BRepAdaptor_Surface(face.wrapped)
        surface_type = adaptor.GetType()

        if surface_type == GeomAbs_SurfaceType.GeomAbs_Plane:
            plane = adaptor.Plane()
            origin = plane.Location()
            normal = plane.Axis().Direction()
            # BRepAdaptor_Surface's underlying Geom_Plane axis is independent
            # of the TopoDS_Face's topological orientation, so a REVERSED
            # face would otherwise yield an inward-pointing "outward" normal.
            sign = -1.0 if face.wrapped.Orientation() == TopAbs_Orientation.TopAbs_REVERSED else 1.0
            features.append(
                CADFeature(
                    id=ids.next_id(FeatureType.PLANE),
                    type=FeatureType.PLANE,
                    nominal_location=Vector3(x=origin.X(), y=origin.Y(), z=origin.Z()),
                    nominal_vector=Vector3(
                        x=normal.X() * sign, y=normal.Y() * sign, z=normal.Z() * sign
                    ),
                    bounding_box=_face_bbox(face),
                )
            )

        elif surface_type == GeomAbs_SurfaceType.GeomAbs_Cylinder:
            cyl = adaptor.Cylinder()
            radius = cyl.Radius()
            loc = cyl.Axis().Location()
            direction = cyl.Axis().Direction()
            axis_location = Vector3(x=loc.X(), y=loc.Y(), z=loc.Z())
            axis_direction = Vector3(x=direction.X(), y=direction.Y(), z=direction.Z())

            u_sweep = abs(adaptor.LastUParameter() - adaptor.FirstUParameter())
            v_min, v_max = adaptor.FirstVParameter(), adaptor.LastVParameter()
            depth = abs(v_max - v_min)

            if u_sweep >= _FULL_SWEEP_RAD:
                center = _axis_point_at(axis_location, axis_direction, (v_min + v_max) / 2)
                features.append(
                    CADFeature(
                        id=ids.next_id(FeatureType.CYLINDER),
                        type=FeatureType.CYLINDER,
                        nominal_location=center,
                        nominal_vector=axis_direction,
                        diameter=radius * 2,
                        depth=depth,
                        bounding_box=_face_bbox(face),
                    )
                )
                start = _axis_point_at(axis_location, axis_direction, v_min)
                end = _axis_point_at(axis_location, axis_direction, v_max)
                covered_bore_keys.add(_round_key(start, radius))
                covered_bore_keys.add(_round_key(end, radius))
            elif _SLOT_ENDCAP_SWEEP_MIN_RAD <= u_sweep <= _SLOT_ENDCAP_SWEEP_MAX_RAD:
                slot_candidates.append(
                    _PartialCylinder(
                        face=face,
                        radius=radius,
                        axis_location=axis_location,
                        axis_direction=axis_direction,
                        v_min=v_min,
                        v_max=v_max,
                    )
                )
            # Partial cylinders outside both windows (e.g. small fillets/rounds)
            # are intentionally not extracted as inspectable features.

        elif surface_type == GeomAbs_SurfaceType.GeomAbs_Cone:
            cone = adaptor.Cone()
            loc = cone.Axis().Location()
            direction = cone.Axis().Direction()
            features.append(
                CADFeature(
                    id=ids.next_id(FeatureType.CONE),
                    type=FeatureType.CONE,
                    nominal_location=Vector3(x=loc.X(), y=loc.Y(), z=loc.Z()),
                    nominal_vector=Vector3(x=direction.X(), y=direction.Y(), z=direction.Z()),
                    radius=cone.RefRadius(),
                    bounding_box=_face_bbox(face),
                )
            )

    return features, slot_candidates, covered_bore_keys


def _round_key(point: Vector3, radius: float) -> tuple[float, float, float, float]:
    r = _LENGTH_TOL_MM
    return (
        round(point.x / r) * r,
        round(point.y / r) * r,
        round(point.z / r) * r,
        round(radius / r) * r,
    )


def _partial_cylinder_center(pc: _PartialCylinder) -> Vector3:
    return _axis_point_at(pc.axis_location, pc.axis_direction, (pc.v_min + pc.v_max) / 2)


def _nearest_compatible(
    i: int, pool: set[int], candidates: list[_PartialCylinder]
) -> int | None:
    """Nearest candidate to `i` (by axis-center distance) among `pool`,
    restricted to matching radius + parallel axis -- the geometric
    prerequisites for being the two end-caps of one slot."""
    a = candidates[i]
    a_center = _partial_cylinder_center(a)
    best_j, best_dist = None, math.inf
    for j in pool:
        if j == i:
            continue
        b = candidates[j]
        if abs(a.radius - b.radius) > _LENGTH_TOL_MM:
            continue
        if not _directions_parallel(a.axis_direction, b.axis_direction):
            continue
        dist = _distance(a_center, _partial_cylinder_center(b))
        if dist < best_dist:
            best_dist, best_j = dist, j
    return best_j


def _build_slot_feature(
    a: _PartialCylinder, b: _PartialCylinder, ids: _IdAllocator
) -> CADFeature:
    a_center = _partial_cylinder_center(a)
    b_center = _partial_cylinder_center(b)
    midpoint = Vector3(
        x=(a_center.x + b_center.x) / 2,
        y=(a_center.y + b_center.y) / 2,
        z=(a_center.z + b_center.z) / 2,
    )
    length = _distance(a_center, b_center)
    if length < _LENGTH_TOL_MM:
        long_axis = a.axis_direction
    else:
        long_axis = Vector3(
            x=(b_center.x - a_center.x) / length,
            y=(b_center.y - a_center.y) / length,
            z=(b_center.z - a_center.z) / length,
        )
    return CADFeature(
        id=ids.next_id(FeatureType.SLOT),
        type=FeatureType.SLOT,
        nominal_location=midpoint,
        nominal_vector=long_axis,
        diameter=a.radius * 2,
        depth=abs(a.v_max - a.v_min),
        bounding_box=None,
    )


def _pair_slot_candidates(
    candidates: list[_PartialCylinder], ids: _IdAllocator
) -> list[CADFeature]:
    """Pair up half-cylindrical end-cap faces into SLOT features.

    Two candidates are paired only when they are each other's nearest
    compatible match (mutual nearest neighbor), not just whichever is
    nearest from one side -- a simple greedy "nearest from i" pass can
    mis-pair end-caps across two closely-spaced parallel slots (matching A's
    end to B's end instead of A's own far end) without ever logging a
    warning, since every candidate still finds *some* match. Requiring
    mutuality catches that case: a wrong cross-pairing is asymmetric (A's
    nearest is B, but B's nearest is its own true partner), so it's skipped
    and both ends fall through to the "could not be paired" warning instead
    of silently producing two wrong slots.
    """
    slots: list[CADFeature] = []
    remaining = set(range(len(candidates)))

    progressed = True
    while progressed and len(remaining) > 1:
        progressed = False
        for i in sorted(remaining):
            j = _nearest_compatible(i, remaining, candidates)
            if j is None:
                continue
            back = _nearest_compatible(j, remaining, candidates)
            if back != i:
                continue  # not mutual -- leave both for a later/no pairing
            slots.append(_build_slot_feature(candidates[i], candidates[j], ids))
            remaining.discard(i)
            remaining.discard(j)
            progressed = True
            break  # restart the scan since `remaining` changed

    for i in remaining:
        a = candidates[i]
        logger.warning(
            "Partial cylindrical face at {} (r={:.3f}) could not be paired "
            "into a SLOT feature; skipping.",
            _partial_cylinder_center(a).as_tuple(),
            a.radius,
        )

    return slots


def _extract_circle_edges(
    shape: cadquery.Shape,  # type: ignore[name-defined]  # noqa: F821
    covered_bore_keys: set[tuple[float, float, float, float]],
    ids: _IdAllocator,
) -> list[CADFeature]:
    """Extract circular edges that are not the rim of an already-extracted
    cylindrical bore (e.g. a scribed circle on a flat/sheet part).
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    circles: list[CADFeature] = []
    seen: set[tuple[float, float, float, float]] = set()

    for edge in shape.Edges():
        if edge.geomType() != "CIRCLE":
            continue
        adaptor = BRepAdaptor_Curve(edge.wrapped)
        circ = adaptor.Circle()
        radius = circ.Radius()
        loc = circ.Location()
        center = Vector3(x=loc.X(), y=loc.Y(), z=loc.Z())
        direction = circ.Axis().Direction()
        normal = Vector3(x=direction.X(), y=direction.Y(), z=direction.Z())

        full_sweep = abs(adaptor.LastParameter() - adaptor.FirstParameter()) >= _FULL_SWEEP_RAD
        if not full_sweep:
            continue  # arc, not a full circle -- not an inspectable circle feature

        key = _round_key(center, radius)
        if key in covered_bore_keys or key in seen:
            continue
        seen.add(key)

        circles.append(
            CADFeature(
                id=ids.next_id(FeatureType.CIRCLE),
                type=FeatureType.CIRCLE,
                nominal_location=center,
                nominal_vector=normal,
                diameter=radius * 2,
            )
        )

    return circles


def extract_features(shape: cadquery.Shape) -> list[CADFeature]:  # type: ignore[name-defined]  # noqa: F821
    """Walk all faces/edges of the shape and extract planes, cylinders,
    cones, slots, and standalone circles as CADFeature objects.
    """
    ids = _IdAllocator()
    features, slot_candidates, covered_bore_keys = _classify_faces(shape, ids)
    features.extend(_pair_slot_candidates(slot_candidates, ids))
    features.extend(_extract_circle_edges(shape, covered_bore_keys, ids))
    return features


def parse_step_file(path: Path) -> list[CADFeature]:
    """Top-level entry point: STEP file path -> list of CADFeature."""
    logger.info("Parsing STEP file: {}", path)
    shape = load_step_file(path)
    features = extract_features(shape)
    logger.info("Extracted {} features from {}", len(features), path.name)
    return features
