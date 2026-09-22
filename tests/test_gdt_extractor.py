"""Tests for gdt_extractor.

match_callouts_to_features is pure logic and tested directly. extract_gdt_callouts
needs a live Anthropic API call, so it's tested with a mocked client that
returns a canned tool_use response, verifying only that CMM-Gen's own
parsing of that response is correct.
"""

from unittest.mock import MagicMock

import pytest

from cmm_gen.gdt_extractor import extract_gdt_callouts, match_callouts_to_features
from cmm_gen.models import (
    CADFeature,
    DatumReference,
    FeatureType,
    GDTCallout,
    GDTCharacteristic,
    Vector3,
)


def _hole(id_: str, x: float, y: float, z: float = 0.0, diameter: float = 8.0) -> CADFeature:
    return CADFeature(
        id=id_,
        type=FeatureType.CYLINDER,
        nominal_location=Vector3(x=x, y=y, z=z),
        nominal_vector=Vector3(x=0, y=0, z=1),
        diameter=diameter,
    )


def _plane(id_: str, x: float, y: float, z: float) -> CADFeature:
    return CADFeature(
        id=id_,
        type=FeatureType.PLANE,
        nominal_location=Vector3(x=x, y=y, z=z),
        nominal_vector=Vector3(x=0, y=0, z=1),
    )


def test_match_position_callout_to_nearest_hole() -> None:
    features = [_hole("CYL_001", 15.0, 0.0), _hole("CYL_002", -15.0, 10.0)]
    callout = GDTCallout(
        id="FCF_001",
        characteristic=GDTCharacteristic.POSITION,
        tolerance_value=0.25,
        diameter_zone=True,
        datums=[DatumReference(label="A")],
        basic_dimensions=Vector3(x=15.02, y=0.01, z=0.0),
    )
    result = match_callouts_to_features([callout], features)
    assert result[0].mapped_feature_id == "CYL_001"
    assert result[0].match_confidence is not None
    assert result[0].match_confidence > 0.9


def test_match_leaves_unmapped_when_too_far() -> None:
    features = [_hole("CYL_001", 15.0, 0.0)]
    callout = GDTCallout(
        id="FCF_001",
        characteristic=GDTCharacteristic.POSITION,
        tolerance_value=0.25,
        basic_dimensions=Vector3(x=100.0, y=100.0, z=0.0),
    )
    result = match_callouts_to_features([callout], features)
    assert result[0].mapped_feature_id is None


def test_match_restricts_candidates_by_characteristic() -> None:
    # A FLATNESS callout should never match a hole even though it sits
    # exactly at the basic-dimension location -- flatness only applies to
    # planar features, so the farther-but-still-in-threshold plane must win.
    features = [_hole("CYL_001", 0.0, 0.0), _plane("PLN_001", 2.0, 2.0, 0.0)]
    callout = GDTCallout(
        id="FCF_001",
        characteristic=GDTCharacteristic.FLATNESS,
        tolerance_value=0.05,
        basic_dimensions=Vector3(x=0.0, y=0.0, z=0.0),
    )
    result = match_callouts_to_features([callout], features)
    assert result[0].mapped_feature_id == "PLN_001"


def test_match_unambiguous_single_candidate_without_basic_dims() -> None:
    features = [_plane("PLN_001", 0.0, 0.0, 0.0)]
    callout = GDTCallout(
        id="FCF_001", characteristic=GDTCharacteristic.FLATNESS, tolerance_value=0.05
    )
    result = match_callouts_to_features([callout], features)
    assert result[0].mapped_feature_id == "PLN_001"
    assert result[0].match_confidence == pytest.approx(0.5)


def test_match_leaves_unmapped_when_ambiguous_and_no_basic_dims() -> None:
    features = [_plane("PLN_001", 0.0, 0.0, 0.0), _plane("PLN_002", 10.0, 0.0, 0.0)]
    callout = GDTCallout(
        id="FCF_001", characteristic=GDTCharacteristic.FLATNESS, tolerance_value=0.05
    )
    result = match_callouts_to_features([callout], features)
    assert result[0].mapped_feature_id is None


def test_extract_gdt_callouts_parses_mocked_tool_response(tmp_path) -> None:
    image_path = tmp_path / "blueprint.png"
    from PIL import Image

    Image.new("RGB", (10, 10)).save(image_path)

    tool_use = MagicMock()
    tool_use.__class__.__name__ = "ToolUseBlock"
    tool_use.input = {
        "callouts": [
            {
                "characteristic": "POSITION",
                "tolerance_value": 0.25,
                "diameter_zone": True,
                "datums": [{"label": "A"}, {"label": "B"}, {"label": "C"}],
                "basic_dimensions": {"x": 15.0, "y": 0.0, "z": 0.0},
                "defines_datum": None,
            }
        ]
    }

    from anthropic.types import ToolUseBlock

    real_tool_use = ToolUseBlock.model_construct(
        id="toolu_1", type="tool_use", name="report_gdt_callouts", input=tool_use.input
    )

    response = MagicMock()
    response.content = [real_tool_use]

    client = MagicMock()
    client.messages.create.return_value = response

    callouts = extract_gdt_callouts(image_path, client=client)

    assert len(callouts) == 1
    assert callouts[0].characteristic == GDTCharacteristic.POSITION
    assert callouts[0].tolerance_value == 0.25
    assert [d.label for d in callouts[0].datums] == ["A", "B", "C"]
    assert callouts[0].basic_dimensions == Vector3(x=15.0, y=0.0, z=0.0)
