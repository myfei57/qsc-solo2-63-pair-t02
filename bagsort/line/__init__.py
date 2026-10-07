"""In-transit position tracking and interception adjudication."""

from __future__ import annotations

from .tracker import (
    DEFAULT_BELT_SPEED,
    DEFAULT_DIVERT_POINTS,
    DEFAULT_INTERCEPT_MARGIN,
    LineTracker,
)

__all__ = [
    "DEFAULT_BELT_SPEED",
    "DEFAULT_DIVERT_POINTS",
    "DEFAULT_INTERCEPT_MARGIN",
    "LineTracker",
]
