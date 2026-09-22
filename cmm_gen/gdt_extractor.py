"""Extracts GD&T callouts, datums, and tolerance blocks from a blueprint
(PDF or image) using Claude's vision capabilities, then maps each callout to
a CADFeature produced by cad_parser.

Convention: ASME Y14.5 (per your answer to the clarifying questions) --
datum labels are single letters (A, B, C, ...) in primary/secondary/
tertiary precedence order as they appear reading a feature control frame
left-to-right, and material condition modifiers are MMC/LMC/RFS (RFS is the
default when no circled modifier is present).

Matching strategy: ASME Y14.5 position tolerances are always referenced to
BASIC (boxed, untoleranced) dimensions locating the feature -- the same
numbers a programmer would use to find the corresponding CAD feature by eye.
`match_callouts_to_features` therefore matches each callout's extracted
`basic_dimensions` against CADFeature.nominal_location (restricted to
geometrically plausible feature types for that GD&T characteristic) rather
than any pixel/CAD projection, which this module doesn't have access to.
Callouts with no legible basic dimensions, or whose nearest candidate is
farther than `_MATCH_DISTANCE_THRESHOLD_MM` away, are left unmapped and
logged as a warning rather than force-matched.
"""

from __future__ import annotations

import io
import json
import math
from pathlib import Path

from anthropic import Anthropic
from anthropic.types import ToolUseBlock
from loguru import logger

from cmm_gen.models import (
    CADFeature,
    DatumReference,
    FeatureType,
    GDTCallout,
    GDTCharacteristic,
    MaterialCondition,
    Vector3,
)

DEFAULT_VISION_MODEL = "claude-opus-5"
_MATCH_DISTANCE_THRESHOLD_MM = 5.0

# Which CADFeature types are geometrically plausible targets for each GD&T
# characteristic. Used to narrow the candidate pool before nearest-basic-
# dimension matching.
_CANDIDATE_FEATURE_TYPES: dict[GDTCharacteristic, tuple[FeatureType, ...]] = {
    GDTCharacteristic.POSITION: (FeatureType.CIRCLE, FeatureType.CYLINDER, FeatureType.SLOT),
    GDTCharacteristic.FLATNESS: (FeatureType.PLANE,),
    GDTCharacteristic.PERPENDICULARITY: (FeatureType.PLANE, FeatureType.CYLINDER),
    GDTCharacteristic.PARALLELISM: (FeatureType.PLANE, FeatureType.CYLINDER),
    GDTCharacteristic.ANGULARITY: (FeatureType.PLANE, FeatureType.CYLINDER),
    GDTCharacteristic.PROFILE_SURFACE: (FeatureType.PLANE, FeatureType.CYLINDER, FeatureType.CONE),
    GDTCharacteristic.PROFILE_LINE: (FeatureType.PLANE, FeatureType.CYLINDER, FeatureType.CONE),
    GDTCharacteristic.CIRCULARITY: (FeatureType.CYLINDER, FeatureType.CONE),
    GDTCharacteristic.CYLINDRICITY: (FeatureType.CYLINDER,),
    GDTCharacteristic.CONCENTRICITY: (FeatureType.CYLINDER, FeatureType.CIRCLE),
    GDTCharacteristic.RUNOUT: (FeatureType.CYLINDER, FeatureType.CONE),
    GDTCharacteristic.TOTAL_RUNOUT: (FeatureType.CYLINDER, FeatureType.CONE),
    GDTCharacteristic.STRAIGHTNESS: (FeatureType.PLANE, FeatureType.CYLINDER),
    GDTCharacteristic.SYMMETRY: (FeatureType.SLOT, FeatureType.PLANE),
    GDTCharacteristic.PLUS_MINUS: tuple(FeatureType),
}

_EXTRACTION_TOOL = {
    "name": "report_gdt_callouts",
    "description": (
        "Report every GD&T feature control frame and its associated basic "
        "dimensions found on this blueprint page, per ASME Y14.5."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "callouts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "characteristic": {
                            "type": "string",
                            "enum": [c.value for c in GDTCharacteristic],
                            "description": "The GD&T symbol/characteristic in the frame.",
                        },
                        "tolerance_value": {
                            "type": "number",
                            "description": "The numeric tolerance value, in the drawing's units.",
                        },
                        "diameter_zone": {
                            "type": "boolean",
                            "description": "True if the tolerance zone has a diameter (⌀) symbol.",
                        },
                        "datums": {
                            "type": "array",
                            "description": "Datum references in the frame, primary first.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "material_condition": {
                                        "type": "string",
                                        "enum": [m.value for m in MaterialCondition],
                                    },
                                },
                                "required": ["label"],
                            },
                        },
                        "basic_dimensions": {
                            "type": "object",
                            "description": (
                                "The boxed/basic X,Y (and Z if given) dimensions locating "
                                "this feature, if legible on the drawing."
                            ),
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "z": {"type": "number"},
                            },
                        },
                        "defines_datum": {
                            "type": ["string", "null"],
                            "description": (
                                "If a datum feature symbol (e.g. a boxed 'A') is attached "
                                "to this same feature, its label; else null."
                            ),
                        },
                        "notes": {"type": ["string", "null"]},
                    },
                    "required": ["characteristic", "tolerance_value"],
                },
            }
        },
        "required": ["callouts"],
    },
}


class GDTExtractionError(RuntimeError):
    """Raised when the blueprint cannot be read or Claude's response cannot be parsed."""


def load_blueprint_pages(path: Path, dpi: int = 300) -> list[bytes]:
    """Return a list of PNG-encoded page images for a PDF or image blueprint.

    A single image file returns a one-element list.
    """
    from PIL import Image

    if path.suffix.lower() == ".pdf":
        from pdf2image import convert_from_path

        pages = convert_from_path(str(path), dpi=dpi)
    else:
        pages = [Image.open(path)]

    encoded: list[bytes] = []
    for page in pages:
        buf = io.BytesIO()
        page.convert("RGB").save(buf, format="PNG")
        encoded.append(buf.getvalue())
    return encoded


def _parse_callout_dict(raw: dict, index: int) -> GDTCallout:  # type: ignore[type-arg]
    datums = [
        DatumReference(
            label=d["label"],
            material_condition=MaterialCondition(d.get("material_condition", "RFS")),
        )
        for d in raw.get("datums", [])
    ]
    basic_dims_raw = raw.get("basic_dimensions")
    basic_dimensions = (
        Vector3(
            x=basic_dims_raw.get("x", 0.0),
            y=basic_dims_raw.get("y", 0.0),
            z=basic_dims_raw.get("z", 0.0),
        )
        if basic_dims_raw
        else None
    )
    return GDTCallout(
        id=f"FCF_{index:03d}",
        characteristic=GDTCharacteristic(raw["characteristic"]),
        tolerance_value=float(raw["tolerance_value"]),
        diameter_zone=bool(raw.get("diameter_zone", False)),
        datums=datums,
        basic_dimensions=basic_dimensions,
        defines_datum=raw.get("defines_datum"),
        notes=raw.get("notes"),
    )


def extract_gdt_callouts(
    blueprint_path: Path, client: Anthropic | None = None
) -> list[GDTCallout]:
    """Send blueprint page images to Claude Vision and parse GD&T callouts,
    datum definitions, and tolerance blocks from the response.
    """
    client = client or Anthropic()
    pages = load_blueprint_pages(blueprint_path)
    logger.info("Extracting GD&T from {} page(s) of {}", len(pages), blueprint_path.name)

    all_callouts: list[GDTCallout] = []
    for page_num, page_bytes in enumerate(pages, start=1):
        import base64

        image_b64 = base64.standard_b64encode(page_bytes).decode("ascii")
        # The SDK's typed overloads expect its own TypedDict/Iterable params
        # rather than plain dict literals; this call is structurally correct
        # against the documented Messages API but doesn't satisfy mypy's
        # strict overload matching without using the SDK's param builders.
        response = client.messages.create(  # type: ignore[call-overload]
            model=DEFAULT_VISION_MODEL,
            max_tokens=4096,
            tools=[_EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "report_gdt_callouts"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Extract every GD&T feature control frame and its basic "
                                "dimensions from this blueprint page, per ASME Y14.5."
                            ),
                        },
                    ],
                }
            ],
        )

        tool_use = next(
            (block for block in response.content if isinstance(block, ToolUseBlock)), None
        )
        if tool_use is None:
            raise GDTExtractionError(
                f"Page {page_num} of {blueprint_path.name}: Claude did not return a "
                "report_gdt_callouts tool call."
            )

        raw_input = tool_use.input
        parsed_input: dict = (  # type: ignore[type-arg]
            json.loads(raw_input) if isinstance(raw_input, str) else raw_input
        )
        raw_callouts = parsed_input.get("callouts", [])

        for raw in raw_callouts:
            try:
                all_callouts.append(_parse_callout_dict(raw, len(all_callouts) + 1))
            except (KeyError, ValueError) as exc:
                logger.warning(
                    "Page {} of {}: could not parse a reported callout ({}): {}",
                    page_num,
                    blueprint_path.name,
                    exc,
                    raw,
                )

    return all_callouts


def _distance(a: Vector3, b: Vector3) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def match_callouts_to_features(
    callouts: list[GDTCallout], features: list[CADFeature]
) -> list[GDTCallout]:
    """Set `mapped_feature_id` and `match_confidence` on each callout by
    correlating its basic dimensions with CADFeature nominal locations,
    restricted to feature types that are geometrically plausible for the
    callout's characteristic.

    Any callout that cannot be confidently matched is logged as a WARNING and
    left unmapped (mapped_feature_id=None) rather than guessed -- downstream
    consumers (pcdmis_generator) must treat unmapped callouts as a hard error
    before final export.
    """
    matched: list[GDTCallout] = []

    for callout in callouts:
        candidate_types = _CANDIDATE_FEATURE_TYPES.get(callout.characteristic, tuple(FeatureType))
        candidates = [f for f in features if f.type in candidate_types]

        if not candidates:
            logger.warning(
                "Callout {} ({}) has no candidate features of type {}; leaving unmapped.",
                callout.id,
                callout.characteristic.value,
                [t.value for t in candidate_types],
            )
            matched.append(callout)
            continue

        if callout.basic_dimensions is not None:
            best = min(
                candidates, key=lambda f: _distance(f.nominal_location, callout.basic_dimensions)  # type: ignore[arg-type]
            )
            distance = _distance(best.nominal_location, callout.basic_dimensions)
            if distance <= _MATCH_DISTANCE_THRESHOLD_MM:
                confidence = max(0.0, 1.0 - distance / _MATCH_DISTANCE_THRESHOLD_MM)
                matched.append(
                    callout.model_copy(
                        update={"mapped_feature_id": best.id, "match_confidence": confidence}
                    )
                )
                continue
            logger.warning(
                "Callout {} ({}): nearest candidate {} is {:.2f}mm away, over the "
                "{}mm match threshold; leaving unmapped.",
                callout.id,
                callout.characteristic.value,
                best.id,
                distance,
                _MATCH_DISTANCE_THRESHOLD_MM,
            )
            matched.append(callout)
            continue

        if len(candidates) == 1:
            logger.warning(
                "Callout {} ({}) has no basic dimensions to match against; matched to "
                "the single unambiguous candidate {} at low confidence.",
                callout.id,
                callout.characteristic.value,
                candidates[0].id,
            )
            matched.append(
                callout.model_copy(
                    update={"mapped_feature_id": candidates[0].id, "match_confidence": 0.5}
                )
            )
            continue

        logger.warning(
            "Callout {} ({}) has no basic dimensions and {} ambiguous candidates; "
            "leaving unmapped.",
            callout.id,
            callout.characteristic.value,
            len(candidates),
        )
        matched.append(callout)

    return matched


def extract_and_map(
    blueprint_path: Path, features: list[CADFeature], client: Anthropic | None = None
) -> list[GDTCallout]:
    """Top-level entry point: blueprint path + parsed features -> mapped callouts."""
    callouts = extract_gdt_callouts(blueprint_path, client=client)
    mapped = match_callouts_to_features(callouts, features)
    unmapped = [c for c in mapped if c.mapped_feature_id is None]
    if unmapped:
        logger.warning(
            "{} of {} callouts could not be mapped to a CAD feature: {}",
            len(unmapped),
            len(mapped),
            [c.id for c in unmapped],
        )
    return mapped
