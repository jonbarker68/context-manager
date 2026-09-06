from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import CalendarEvent, CalendarProvider
from .conference import extract_conference_url


class EventKitProvider(CalendarProvider):
    """macOS Calendar backend implemented by the existing Swift helper."""

    name = "eventkit"

    def __init__(self, helper: Path):
        self.helper = helper.expanduser().resolve()

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        if not self.helper.exists():
            raise RuntimeError(
                "EventKit calendar helper not found:\n"
                f"  {self.helper}\n\n"
                "Build native/calendar-query/calendar-query first."
            )

        # The helper queries whole local calendar days. Ask for enough days
        # to include `end`; Python then clips the returned events exactly.
        local_start = start.astimezone()
        local_end = end.astimezone()
        days = max(1, (local_end.date() - local_start.date()).days + 1)

        result = subprocess.run(
            [str(self.helper), str(days)],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            message = result.stderr.strip() or "Unknown EventKit error"
            raise RuntimeError(f"Unable to query macOS Calendar:\n{message}")

        try:
            raw_events = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Calendar helper returned invalid JSON:\n"
                f"{result.stdout}\n\n{exc}"
            ) from exc

        normalised: list[CalendarEvent] = []
        for event in raw_events:
            item = {
                "id": event.get("id"),
                "title": event.get("title") or "",
                "start": event.get("start"),
                "end": event.get("end"),
                "notes": event.get("notes") or "",
                "attendees": event.get("attendees") or [],
                "calendar": event.get("calendar") or "",
                "provider": self.name,
            }

            if not item["start"] or not item["end"]:
                continue

            event_start = datetime.fromisoformat(
                str(item["start"]).replace("Z", "+00:00")
            )
            event_end = datetime.fromisoformat(
                str(item["end"]).replace("Z", "+00:00")
            )

            if event_start < end and event_end > start:
                normalised.append(item)

        return normalised

    def describe(self) -> str:
        return f"eventkit ({self.helper})"
