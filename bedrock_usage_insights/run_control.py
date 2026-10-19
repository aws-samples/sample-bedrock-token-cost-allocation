"""Analysis-run lifecycle and accounting boundary."""

from __future__ import annotations

from typing import Protocol, Sequence

from .models import AnalysisRun, ErrorRecord, InsightConfiguration, InsightsResult


class RunController(Protocol):
    """Port for one-run accounting, failure isolation, and status transitions."""

    def start(
        self,
        configuration: InsightConfiguration,
        *,
        analysis_run_id: str,
    ) -> AnalysisRun:
        """Create exactly one pending/running run record before source reads."""
        ...

    def finish(
        self,
        run: AnalysisRun,
        results: Sequence[InsightsResult],
        errors: Sequence[ErrorRecord],
    ) -> AnalysisRun:
        """Reconcile counters and derive completed, partial, or failed status."""
        ...


__all__ = ["RunController"]
