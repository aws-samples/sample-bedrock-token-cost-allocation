"""Prompt-quality assessment boundary for permitted content previews."""

from __future__ import annotations

from typing import Protocol

from .models import (
    InsightConfiguration,
    PromptQualityAssessment,
    SensitiveContent,
)


class PromptAssessor(Protocol):
    """Port for independently configured prompt-quality dimensions."""

    def assess(
        self,
        content: SensitiveContent | None,
        configuration: InsightConfiguration,
    ) -> PromptQualityAssessment:
        """Assess prompt characteristics or mark affected dimensions unavailable."""
        ...


__all__ = ["PromptAssessor"]
