"""Centralized logging setup (loguru) so every module logs consistently.

kinematics_validator and the feature<->GD&T matcher in gdt_extractor are the
two places where silent failure is most dangerous (an unmatched callout or an
unreachable feature must never be dropped quietly), so both log at WARNING or
above whenever they cannot resolve something, and the CLI surfaces those as a
non-zero exit code / summary table rather than letting them pass silently.
"""

from __future__ import annotations

import sys

from loguru import logger

_CONFIGURED = False


def configure_logging(verbose: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{module}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
        ),
    )
    _CONFIGURED = True
