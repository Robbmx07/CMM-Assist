"""Extracts GD&T callouts, datums, and tolerance blocks from a blueprint
(PDF or image) using Claude's vision capabilities, then maps each callout to
a CADFeature produced by cad_parser.

STATUS: PDF/image ingestion and the raw Claude call are safe to build now.
The extraction prompt's output schema, and the feature-matching heuristic
(nearest feature by basic dimensions vs. explicit callout->feature linking on
the blueprint), are left as TODOs pending your answers on datum/tolerance
conventions -- see the clarifying questions.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from cmm_gen.models import CADFeature, GDTCallout

DEFAULT_VISION_MODEL = "claude-opus-5"


class GDTExtractionError(RuntimeError):
    """Raised when the blueprint cannot be read or Claude's response cannot be parsed."""


def load_blueprint_pages(path: Path, dpi: int = 300) -> list[bytes]:
    """Return a list of PNG-encoded page images for a PDF or image blueprint.

    A single image file returns a one-element list.
    """
    import io

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


def extract_gdt_callouts(blueprint_path: Path) -> list[GDTCallout]:
    """Send blueprint page images to Claude Vision and parse GD&T callouts,
    datum definitions, and tolerance blocks from the response.

    TODO(pending your answers): finalize the structured-output schema/prompt
    (datum hierarchy convention, how compound/composite tolerance frames are
    represented) before wiring this up to the anthropic client.
    """
    raise NotImplementedError(
        "extract_gdt_callouts: implementation pending clarification on "
        "datum hierarchy and tolerance mapping conventions."
    )


def match_callouts_to_features(
    callouts: list[GDTCallout], features: list[CADFeature]
) -> list[GDTCallout]:
    """Set `mapped_feature_id` and `match_confidence` on each callout by
    correlating its basic dimensions / nearby geometry with CADFeature
    nominal locations.

    Any callout that cannot be confidently matched is logged as a WARNING and
    left unmapped (mapped_feature_id=None) rather than guessed -- downstream
    consumers (pcdmis_generator) must treat unmapped callouts as a hard error
    before final export.
    """
    raise NotImplementedError(
        "match_callouts_to_features: implementation pending clarification "
        "on datum hierarchy and tolerance mapping conventions."
    )


def extract_and_map(blueprint_path: Path, features: list[CADFeature]) -> list[GDTCallout]:
    """Top-level entry point: blueprint path + parsed features -> mapped callouts."""
    logger.info("Extracting GD&T from blueprint: {}", blueprint_path)
    callouts = extract_gdt_callouts(blueprint_path)
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
