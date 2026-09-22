"""Tests for kinematics_validator's collision/reachability logic."""

import pytest

from cmm_gen.kinematics_validator import (
    KinematicsValidationError,
    candidate_approach_vectors,
    check_collision,
    check_head_angle_feasible,
    check_travel_limits,
    load_machine_envelope,
    load_probe_config,
    validate_feature,
)
from cmm_gen.models import (
    ApproachVectorStatus,
    BoundingBox,
    CADFeature,
    FeatureType,
    MachineEnvelope,
    ProbeConfig,
    ProbeTip,
    Vector3,
)


@pytest.fixture
def indexable_probe() -> ProbeConfig:
    return load_probe_config("HH-A-T5")


@pytest.fixture
def fixed_probe() -> ProbeConfig:
    return ProbeConfig(
        name="fixed-vertical",
        head_type="fixed",
        tip=ProbeTip(diameter_mm=3.0, stylus_length_mm=20.0),
        a_angle_deg=0.0,
        b_angle_deg=0.0,
    )


@pytest.fixture
def wide_envelope() -> MachineEnvelope:
    return MachineEnvelope(
        x_travel_mm=(-500, 500), y_travel_mm=(-500, 500), z_travel_mm=(-500, 500)
    )


def _plate_top_plane(z: float = 7.5) -> CADFeature:
    return CADFeature(
        id="PLN_001",
        type=FeatureType.PLANE,
        nominal_location=Vector3(x=0, y=0, z=z),
        nominal_vector=Vector3(x=0, y=0, z=1),  # outward normal points up
    )


def _through_hole() -> CADFeature:
    return CADFeature(
        id="CYL_001",
        type=FeatureType.CYLINDER,
        nominal_location=Vector3(x=0, y=0, z=0),
        nominal_vector=Vector3(x=0, y=0, z=1),
        diameter=8.0,
        depth=15.0,
    )


def test_plane_candidate_vector_points_down_into_surface() -> None:
    candidates = candidate_approach_vectors(_plate_top_plane())
    assert len(candidates) == 1
    assert candidates[0].z == pytest.approx(-1.0)  # opposite the +Z outward normal


def test_cylinder_offers_both_axis_directions() -> None:
    candidates = candidate_approach_vectors(_through_hole())
    zs = sorted(c.z for c in candidates)
    assert zs == pytest.approx([-1.0, 1.0])


def test_valid_top_face_approach_passes_all_checks(indexable_probe, wide_envelope) -> None:
    result = validate_feature(_plate_top_plane(), indexable_probe, wide_envelope)
    assert result.status == ApproachVectorStatus.VALID
    assert result.clearance_point.z > result.contact_point.z


def test_fixed_head_rejects_off_axis_feature(fixed_probe, wide_envelope) -> None:
    # Fixed probe is oriented straight down (0,0,-1); a side-facing plane
    # needs a horizontal approach it cannot reach.
    side_plane = CADFeature(
        id="PLN_002",
        type=FeatureType.PLANE,
        nominal_location=Vector3(x=10, y=0, z=0),
        nominal_vector=Vector3(x=1, y=0, z=0),
    )
    result = validate_feature(side_plane, fixed_probe, wide_envelope)
    assert result.status == ApproachVectorStatus.REJECTED_HEAD_ANGLE
    assert "cannot reorient" in result.rejection_reason


def test_travel_limit_rejection(indexable_probe) -> None:
    tight_envelope = MachineEnvelope(
        x_travel_mm=(-5, 5), y_travel_mm=(-5, 5), z_travel_mm=(-5, 5)
    )
    result = validate_feature(_plate_top_plane(z=100.0), indexable_probe, tight_envelope)
    assert result.status == ApproachVectorStatus.REJECTED_TRAVEL_LIMIT


def test_check_collision_flags_blind_spot(indexable_probe, wide_envelope) -> None:
    # A feature near the +X edge whose candidate approach direction (-X,
    # moving from outside toward the surface) is head-angle-feasible
    # (horizontal), but the part's bounding box is deliberately large enough
    # that the clearance point still falls inside it -- e.g. an internal
    # pocket wall with no external clearance in that direction.
    feature = CADFeature(
        id="PLN_003",
        type=FeatureType.PLANE,
        nominal_location=Vector3(x=29, y=0, z=0),
        nominal_vector=Vector3(x=1, y=0, z=0),
    )
    vector = Vector3(x=-1, y=0, z=0)
    part_bbox = BoundingBox(min=Vector3(x=-30, y=-20, z=-7.5), max=Vector3(x=100, y=20, z=7.5))
    ok, reason = check_collision(feature, vector, indexable_probe, wide_envelope, part_bbox)
    assert not ok
    assert "blind spot" in reason


def test_validate_feature_rejects_unreachable_underside(indexable_probe, wide_envelope) -> None:
    # A bottom face whose outward normal points DOWN (-Z): the only
    # candidate approach is straight UP into the part from underneath,
    # which no realistic head can orient to and which also has no fixture
    # clearance -- validate_feature must reject it outright, whichever
    # check catches it first.
    bottom_face = CADFeature(
        id="PLN_004",
        type=FeatureType.PLANE,
        nominal_location=Vector3(x=0, y=0, z=-7.5),
        nominal_vector=Vector3(x=0, y=0, z=-1),
    )
    result = validate_feature(bottom_face, indexable_probe, wide_envelope)
    assert result.status != ApproachVectorStatus.VALID


def test_indexable_head_snaps_to_grid_within_tolerance(indexable_probe) -> None:
    # 7.5deg increments: straight down (A=0) is always on-grid.
    ok, reason = check_head_angle_feasible(Vector3(x=0, y=0, z=-1), indexable_probe)
    assert ok
    assert reason is None


def test_check_travel_limits_direct() -> None:
    envelope = MachineEnvelope(x_travel_mm=(0, 10), y_travel_mm=(0, 10), z_travel_mm=(0, 10))
    ok, _ = check_travel_limits(Vector3(x=5, y=5, z=5), envelope)
    assert ok
    ok, reason = check_travel_limits(Vector3(x=50, y=5, z=5), envelope)
    assert not ok
    assert "travel envelope" in reason


def test_load_probe_config_honors_custom_library_path(tmp_path) -> None:
    # Regression/coverage test: this is the exact mechanism the standalone
    # .exe's --probe-library flag depends on -- the bundled config is baked
    # into the binary at build time, so overriding it by pointing at a
    # different file on disk is the only way exe users can ever replace the
    # placeholder probe specs with their real datasheet values.
    custom_library = tmp_path / "probe_library.yaml"
    custom_library.write_text(
        """
probes:
  MY-CUSTOM-PROBE:
    verified: true
    head_type: fixed
    tip:
      diameter_mm: 1.5
      stylus_length_mm: 12.0
    a_angle_range_deg: [0.0, 0.0]
    b_angle_range_deg: [0.0, 0.0]
"""
    )
    probe = load_probe_config("MY-CUSTOM-PROBE", library_path=custom_library)
    assert probe.tip.diameter_mm == 1.5
    assert probe.tip.stylus_length_mm == 12.0

    with pytest.raises(KinematicsValidationError):
        load_probe_config("HH-A-T5", library_path=custom_library)  # not in this file


def test_load_machine_envelope_honors_custom_library_path(tmp_path) -> None:
    custom_library = tmp_path / "machine_envelope.yaml"
    custom_library.write_text(
        """
machines:
  MY-MACHINE:
    verified: true
    x_travel_mm: [-10.0, 10.0]
    y_travel_mm: [-20.0, 20.0]
    z_travel_mm: [-5.0, 5.0]
"""
    )
    envelope = load_machine_envelope("MY-MACHINE", library_path=custom_library)
    assert envelope.x_travel_mm == (-10.0, 10.0)
    assert envelope.z_travel_mm == (-5.0, 5.0)
