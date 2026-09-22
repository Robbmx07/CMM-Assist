"""Shared data contracts used across every CMM-Gen module.

These types are the seams between cad_parser -> gdt_extractor ->
kinematics_validator -> pcdmis_generator. Keeping them in one place means each
module can be developed and tested independently against the same schema.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Vector3(BaseModel):
    """A point or direction in the part/machine coordinate system."""

    x: float
    y: float
    z: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


class BoundingBox(BaseModel):
    min: Vector3
    max: Vector3


class FeatureType(str, Enum):
    PLANE = "PLANE"
    CYLINDER = "CYLINDER"
    CONE = "CONE"
    CIRCLE = "CIRCLE"
    SLOT = "SLOT"
    SPHERE = "SPHERE"
    LINE = "LINE"
    POINT = "POINT"


class CADFeature(BaseModel):
    """A geometric feature extracted from the STEP file.

    `id` is generated deterministically (e.g. from STEP entity index + type) so
    that re-running the parser on the same file yields stable IDs that
    gdt_extractor and the visualizer can rely on for cross-referencing.
    """

    id: str
    type: FeatureType
    nominal_location: Vector3
    nominal_vector: Vector3  # surface normal (planes) or axis direction (cylinders/cones)
    diameter: float | None = None
    radius: float | None = None
    depth: float | None = None
    bounding_box: BoundingBox | None = None
    step_entity_id: int | None = None
    source_face_ids: list[int] = Field(default_factory=list)


class GDTCharacteristic(str, Enum):
    POSITION = "POSITION"
    FLATNESS = "FLATNESS"
    PERPENDICULARITY = "PERPENDICULARITY"
    PARALLELISM = "PARALLELISM"
    ANGULARITY = "ANGULARITY"
    PROFILE_SURFACE = "PROFILE_SURFACE"
    PROFILE_LINE = "PROFILE_LINE"
    CIRCULARITY = "CIRCULARITY"
    CYLINDRICITY = "CYLINDRICITY"
    CONCENTRICITY = "CONCENTRICITY"
    RUNOUT = "RUNOUT"
    TOTAL_RUNOUT = "TOTAL_RUNOUT"
    STRAIGHTNESS = "STRAIGHTNESS"
    SYMMETRY = "SYMMETRY"
    BASIC_DIMENSION = "BASIC_DIMENSION"
    PLUS_MINUS = "PLUS_MINUS"  # non-GD&T +/- tolerance block


class MaterialCondition(str, Enum):
    MMC = "MMC"
    LMC = "LMC"
    RFS = "RFS"  # regardless of feature size (default, no symbol)


class DatumReference(BaseModel):
    label: str  # e.g. "A", "B", "C"
    material_condition: MaterialCondition = MaterialCondition.RFS


class GDTCallout(BaseModel):
    """A single GD&T callout extracted from the blueprint by gdt_extractor."""

    id: str
    characteristic: GDTCharacteristic
    tolerance_value: float
    diameter_zone: bool = False  # True if tolerance zone is a diameter (⌀ symbol)
    datums: list[DatumReference] = Field(default_factory=list)
    basic_dimensions: Vector3 | None = None
    mapped_feature_id: str | None = None  # set once matched to a CADFeature
    match_confidence: float | None = None  # 0-1, set by the matching step
    source_bbox_px: tuple[int, int, int, int] | None = None  # for visualizer overlay
    defines_datum: str | None = None  # e.g. "A" if a datum feature symbol is attached
    notes: str | None = None


class DatumFeature(BaseModel):
    """A datum feature/target definition (A, B, C, ...) used to build alignments."""

    label: str
    feature_id: str
    precedence: int  # 1 = primary, 2 = secondary, 3 = tertiary


class ProbeTip(BaseModel):
    diameter_mm: float
    stylus_length_mm: float
    material: Literal["ruby", "silicon_nitride", "zirconia"] = "ruby"


class ProbeConfig(BaseModel):
    """Physical probe/head configuration used by kinematics_validator.

    - "fixed": the head holds one orientation (a_angle_deg/b_angle_deg) for
      the whole routine; only approach vectors aligned with that single
      orientation are reachable.
    - "indexable": head can be indexed between discrete A/B positions on
      `index_increment_deg` steps within `a_angle_range_deg`/
      `b_angle_range_deg` (e.g. a Renishaw PH10-style head on 7.5deg steps).
    - "continuous_5axis": head can orient continuously within the A/B range
      (e.g. PH20/REVO-style), no index-step snapping.
    """

    name: str
    head_type: Literal["fixed", "indexable", "continuous_5axis"]
    tip: ProbeTip
    extension_length_mm: float = 0.0
    a_angle_deg: float = 0.0  # head tilt (used as-is when head_type == "fixed")
    b_angle_deg: float = 0.0  # head rotation (used as-is when head_type == "fixed")
    index_increment_deg: float = 7.5  # only meaningful when head_type == "indexable"
    a_angle_range_deg: tuple[float, float] = (-90.0, 105.0)
    b_angle_range_deg: tuple[float, float] = (-180.0, 180.0)
    notes: str | None = None


class MachineEnvelope(BaseModel):
    """Machine travel limits and fixture keep-out, used for collision checks."""

    x_travel_mm: tuple[float, float]
    y_travel_mm: tuple[float, float]
    z_travel_mm: tuple[float, float]
    fixture_keepout_boxes: list[BoundingBox] = Field(default_factory=list)


class ApproachVectorStatus(str, Enum):
    VALID = "VALID"
    REJECTED_COLLISION = "REJECTED_COLLISION"
    REJECTED_HEAD_ANGLE = "REJECTED_HEAD_ANGLE"
    REJECTED_TRAVEL_LIMIT = "REJECTED_TRAVEL_LIMIT"
    REJECTED_BLIND_SPOT = "REJECTED_BLIND_SPOT"


class ApproachVector(BaseModel):
    """A candidate probe approach/retract vector for a feature hit, and its
    validation result from kinematics_validator."""

    feature_id: str
    contact_point: Vector3
    approach_vector: Vector3  # unit vector, points from clearance toward the surface
    clearance_point: Vector3
    retract_point: Vector3
    status: ApproachVectorStatus = ApproachVectorStatus.VALID
    rejection_reason: str | None = None


class InspectionRoutine(BaseModel):
    """The fully resolved, validated routine ready for pcdmis_generator."""

    part_name: str
    features: list[CADFeature]
    callouts: list[GDTCallout]
    datums: list[DatumFeature]
    approach_vectors: list[ApproachVector]
    probe_config: ProbeConfig
    machine_envelope: MachineEnvelope
