"""Deterministic category classification and uncertainty boundary."""

from __future__ import annotations

from typing import Protocol

from .models import ClassificationResult, InsightConfiguration, Signal


class Classifier(Protocol):
    """Port for threshold, precedence, confidence, and unknown-result policy."""

    def classify(
        self,
        signals: tuple[Signal, ...],
        configuration: InsightConfiguration,
    ) -> ClassificationResult:
        """Return a conservative, explainable classification result."""
        ...


__all__ = ["Classifier"]
