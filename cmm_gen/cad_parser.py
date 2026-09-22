"""Parses .STEP files into the CADFeature schema defined in models.py.

Backend: CadQuery (OCP/OpenCascade bindings). Swap to pythonocc-core by
reimplementing `_load_shape` and the per-surface-type extractors below if the
shop standardizes on that stack instead -- everything downstream only depends
on the CADFeature list, not on the CAD kernel used to produce it.

STATUS: interface + bounding-box/plane/cylinder extraction are safe to build
now. Slot and cone recognition, and the STEP-entity-id -> stable feature-id
scheme, are left as TODOs pending your answers on feature-ID conventions
(see the clarifying questions).
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from cmm_gen.models import BoundingBox, CADFeature, FeatureType, Vector3


class CADParseError(RuntimeError):
    """Raised when a STEP file cannot be loaded or contains no recognizable features."""


def load_step_file(path: Path) -> "cadquery.Shape":  # type: ignore[name-defined]  # noqa: F821
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


def extract_features(shape: "cadquery.Shape") -> list[CADFeature]:  # type: ignore[name-defined]  # noqa: F821
    """Walk all faces/edges of the shape and extract planes, cylinders, cones,
    circles, and slots as CADFeature objects.

    TODO(pending your answers): slot recognition (paired parallel planes +
    two half-cylinders vs. a single swept face) and the feature-id scheme.
    """
    raise NotImplementedError(
        "extract_features: implementation pending clarification on feature "
        "ID conventions and slot-recognition rules."
    )


def compute_bounding_box(shape: "cadquery.Shape") -> BoundingBox:  # type: ignore[name-defined]  # noqa: F821
    bb = shape.BoundingBox()
    return BoundingBox(
        min=Vector3(x=bb.xmin, y=bb.ymin, z=bb.zmin),
        max=Vector3(x=bb.xmax, y=bb.ymax, z=bb.zmax),
    )


def parse_step_file(path: Path) -> list[CADFeature]:
    """Top-level entry point: STEP file path -> list of CADFeature."""
    logger.info("Parsing STEP file: {}", path)
    shape = load_step_file(path)
    features = extract_features(shape)
    logger.info("Extracted {} features from {}", len(features), path.name)
    return features
