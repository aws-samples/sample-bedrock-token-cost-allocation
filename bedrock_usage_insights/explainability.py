"""Explainability and derived-result validation boundary."""

from __future__ import annotations

from typing import Protocol

from .models import (
    ClassificationResult,
    InsightConfiguration,
    InsightsResult,
    InvocationRecord,
    PromptQualityAssessment,
)


class ResultBuilder(Protocol):
    """Port for traceable, privacy-filtered result construction."""

    def build(
        self,
        record: InvocationRecord,
        classification: ClassificationResult,
        prompt_quality: PromptQualityAssessment,
        configuration: InsightConfiguration,
    ) -> InsightsResult:
        """Build a result without inventing required traceability metadata."""
        ...

    def validate(
        self,
        result: InsightsResult,
        configuration: InsightConfiguration,
    ) -> tuple[str, ...]:
        """Return bounded validation errors before publication."""
        ...


__all__ = ["ResultBuilder"]
