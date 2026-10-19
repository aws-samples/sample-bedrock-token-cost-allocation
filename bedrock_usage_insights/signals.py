"""Configured signal extraction boundary."""

from __future__ import annotations

from typing import Protocol

from .models import InsightConfiguration, InvocationRecord, Signal


class SignalExtractor(Protocol):
    """Port for allowlisted direct and approved content-derived signals."""

    def extract(
        self,
        record: InvocationRecord,
        configuration: InsightConfiguration,
    ) -> tuple[Signal, ...]:
        """Return only signals declared by the active configuration."""
        ...


__all__ = ["SignalExtractor"]
