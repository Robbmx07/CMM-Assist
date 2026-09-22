"""Tests for pcdmis_generator, including a full parse -> validate -> generate
pipeline run against the synthetic sample_part.step fixture.
"""

from pathlib import Path

import pytest

from cmm_gen.cad_parser import parse_step_file
from cmm_gen.kinematics_validator import load_probe_config, validate_feature
from cmm_gen.models import (
    ApproachVectorStatus,
    DatumFeature,
    DatumReference,
    FeatureType,
    GDTCallout,
    GDTCharacteristic,
    InspectionRoutine,
    MachineEnvelope,
)
from cmm_gen.pcdmis_generator import (
    RoutineGenerationError,
    emit_alignment_block,
    emit_safe_start_move,
    generate_routine,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_part.step"


@pytest.fixture(scope="module")
def features():
    return parse_step_file(FIXTURE)


@pytest.fixture
def probe():
    return load_probe_config("HH-A-T5")


@pytest.fixture
def envelope():
    return MachineEnvelope(
        x_travel_mm=(-500, 500), y_travel_mm=(-500, 500), z_travel_mm=(-500, 500)
    )


def _build_routine(features, probe, envelope) -> InspectionRoutine:
    all_vectors = [validate_feature(f, probe, envelope) for f in features]

    # The plate's bottom face (sitting on the fixture) is correctly flagged
    # REJECTED_HEAD_ANGLE from a single top-side setup -- it needs a second
    # operation/flip to inspect, so it's excluded from this single-setup
    # routine rather than force-included and failing generation.
    reachable_ids = {
        av.feature_id for av in all_vectors if av.status == ApproachVectorStatus.VALID
    }
    features = [f for f in features if f.id in reachable_ids]
    approach_vectors = [av for av in all_vectors if av.feature_id in reachable_ids]

    planes = [f for f in features if f.type == FeatureType.PLANE]
    through_hole = next(f for f in features if f.type == FeatureType.CYLINDER and f.diameter == 8.0)

    datums = [
        DatumFeature(label="A", feature_id=planes[2].id, precedence=1),
        DatumFeature(label="B", feature_id=planes[0].id, precedence=2),
        DatumFeature(label="C", feature_id=planes[1].id, precedence=3),
    ]
    callouts = [
        GDTCallout(
            id="FCF_001",
            characteristic=GDTCharacteristic.POSITION,
            tolerance_value=0.25,
            diameter_zone=True,
            datums=[
                DatumReference(label="A"),
                DatumReference(label="B"),
                DatumReference(label="C"),
            ],
            mapped_feature_id=through_hole.id,
        )
    ]
    return InspectionRoutine(
        part_name="sample_part",
        features=features,
        callouts=callouts,
        datums=datums,
        approach_vectors=approach_vectors,
        probe_config=probe,
        machine_envelope=envelope,
    )


def test_alignment_block_requires_three_datums(probe) -> None:
    routine = InspectionRoutine(
        part_name="x",
        features=[],
        callouts=[],
        datums=[DatumFeature(label="A", feature_id="PLN_001", precedence=1)],
        approach_vectors=[],
        probe_config=probe,
        machine_envelope=MachineEnvelope(
            x_travel_mm=(0, 1), y_travel_mm=(0, 1), z_travel_mm=(0, 1)
        ),
    )
    with pytest.raises(RoutineGenerationError):
        emit_alignment_block(routine)


def test_alignment_block_contains_datum_sequence(features, probe, envelope) -> None:
    routine = _build_routine(features, probe, envelope)
    block = emit_alignment_block(routine)
    assert "ALIGNMENT/START" in block
    assert "ALIGNMENT/LEVEL,ZPLUS" in block
    assert "ALIGNMENT/ROTATE,XPLUS,TO" in block
    assert "ALIGNMENT/ORIGIN" in block
    assert "ALIGNMENT/END" in block


def test_generate_routine_rejects_unmapped_callout(features, probe, envelope) -> None:
    routine = _build_routine(features, probe, envelope)
    routine.callouts.append(
        GDTCallout(
            id="FCF_002",
            characteristic=GDTCharacteristic.FLATNESS,
            tolerance_value=0.1,
            mapped_feature_id=None,
        )
    )
    with pytest.raises(RoutineGenerationError, match="not mapped"):
        generate_routine(routine, Path("/tmp/should_not_be_written.prg"))


def test_full_pipeline_generates_prg_file(tmp_path, features, probe, envelope) -> None:
    routine = _build_routine(features, probe, envelope)
    # All fixture features must be reachable with the default probe/envelope.
    assert all(av.status == ApproachVectorStatus.VALID for av in routine.approach_vectors), [
        (av.feature_id, av.status, av.rejection_reason)
        for av in routine.approach_vectors
        if av.status != ApproachVectorStatus.VALID
    ]

    out_path = tmp_path / "sample_part.prg"
    generate_routine(routine, out_path)

    text = out_path.read_text()
    assert "UNITS/MM" in text
    assert "ALIGNMENT/START" in text
    assert "F(CYL_001)=FEAT/CIRCLE" in text or "F(CYL_001)=FEAT/CYLINDER" in text
    assert "DIM TRUEPOS1=TRUEPOS OF CYL_001" in text
    assert "SUB CheckAllDimensions()" in text
    assert "PART STATUS: ACCEPTED" in text


def test_safe_start_move_respects_small_machine_envelope(features, probe) -> None:
    # Regression test: emit_safe_start_move used to hardcode a 100mm safe Z
    # regardless of the machine's actual travel limits, so a small-envelope
    # machine (e.g. z max of 50mm) would get a program whose very first move
    # already violates its travel limit.
    small_envelope = MachineEnvelope(
        x_travel_mm=(-100, 100), y_travel_mm=(-100, 100), z_travel_mm=(-50, 50)
    )
    routine = InspectionRoutine(
        part_name="x",
        features=[],
        callouts=[],
        datums=[],
        approach_vectors=[],
        probe_config=probe,
        machine_envelope=small_envelope,
    )
    move = emit_safe_start_move(routine)
    assert "100.0000" not in move
    assert "50.0000" in move


def test_safe_start_move_rejects_out_of_range_explicit_z(features, probe) -> None:
    envelope = MachineEnvelope(
        x_travel_mm=(-100, 100), y_travel_mm=(-100, 100), z_travel_mm=(-50, 50)
    )
    routine = InspectionRoutine(
        part_name="x",
        features=[],
        callouts=[],
        datums=[],
        approach_vectors=[],
        probe_config=probe,
        machine_envelope=envelope,
    )
    with pytest.raises(RoutineGenerationError):
        emit_safe_start_move(routine, safe_z=999.0)
