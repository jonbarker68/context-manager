from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any


CalendarEvent = dict[str, Any]


class CalendarProvider(ABC):
    """Backend-neutral source of normalised calendar events."""

    name: str

    @abstractmethod
    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Return timed events intersecting [start, end)."""

    def authenticate(self) -> None:
        """Perform any interactive authentication required by the provider."""
        return None

    def describe(self) -> str:
        return self.name
