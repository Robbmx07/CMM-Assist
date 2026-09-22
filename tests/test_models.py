"""Sanity tests for the shared data contracts in cmm_gen.models."""

from cmm_gen.models import (
    ApproachVector,
    ApproachVectorStatus,
    BoundingBox,
    CADFeature,
    FeatureType,
    Vector3,
)


def test_vector3_as_tuple() -> None:
    v = Vector3(x=1.0, y=2.0, z=3.0)
    assert v.as_tuple() == (1.0, 2.0, 3.0)


def test_cad_feature_round_trip() -> None:
    feature = CADFeature(
        id="CYL_001",
        type=FeatureType.CYLINDER,
        nominal_location=Vector3(x=0, y=0, z=0),
        nominal_vector=Vector3(x=0, y=0, z=1),
        diameter=10.0,
        bounding_box=BoundingBox(
            min=Vector3(x=-5, y=-5, z=0), max=Vector3(x=5, y=5, z=20)
        ),
    )
    payload = feature.model_dump_json()
    restored = CADFeature.model_validate_json(payload)
    assert restored == feature


def test_approach_vector_default_status_is_valid() -> None:
    av = ApproachVector(
        feature_id="PLN_001",
        contact_point=Vector3(x=0, y=0, z=0),
        approach_vector=Vector3(x=0, y=0, z=1),
        clearance_point=Vector3(x=0, y=0, z=10),
        retract_point=Vector3(x=0, y=0, z=10),
    )
    assert av.status == ApproachVectorStatus.VALID
