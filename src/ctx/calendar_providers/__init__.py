from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .base import CalendarEvent, CalendarProvider
from .eventkit import EventKitProvider
from .google import GoogleCalendarProvider


DEFAULT_CONFIG_PATH = Path("~/.config/ctx/config.yaml").expanduser()


def load_calendar_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    if not config_path.exists():
        return {}

    data = yaml.safe_load(config_path.read_text()) or {}
    calendar = data.get("calendar") or {}
    if not isinstance(calendar, dict):
        raise RuntimeError(
            f"'calendar' in {config_path} must be a YAML mapping."
        )
    return calendar


def get_calendar_provider(
    *,
    project_root: Path,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> CalendarProvider:
    config = load_calendar_config(config_path)

    provider_name = (
        os.environ.get("CTX_CALENDAR_PROVIDER")
        or config.get("provider")
        or "eventkit"
    ).strip().casefold()

    if provider_name in {"eventkit", "apple", "mac", "macos"}:
        eventkit = config.get("eventkit") or {}
        helper = eventkit.get(
            "helper",
            project_root / "native" / "calendar-query" / "calendar-query",
        )
        return EventKitProvider(Path(str(helper)).expanduser())

    if provider_name == "google":
        google = config.get("google") or {}

        credentials = Path(
            str(
                google.get(
                    "credentials",
                    "~/.config/ctx/google-calendar-credentials.json",
                )
            )
        ).expanduser()

        token = Path(
            str(
                google.get(
                    "token",
                    "~/.local/share/ctx/google-calendar-token.json",
                )
            )
        ).expanduser()

        calendar_ids = google.get("calendar_ids") or ["primary"]
        if isinstance(calendar_ids, str):
            calendar_ids = [calendar_ids]

        return GoogleCalendarProvider(
            credentials_path=credentials,
            token_path=token,
            calendar_ids=[str(value) for value in calendar_ids],
        )

    raise RuntimeError(
        f"Unknown calendar provider '{provider_name}'. "
        "Use 'eventkit' or 'google'."
    )


def calendar_events_for_days(
    provider: CalendarProvider,
    days: int,
) -> list[CalendarEvent]:
    days = max(1, int(days))
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)
    return provider.events(start, end)
