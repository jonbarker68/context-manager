from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .base import CalendarEvent, CalendarProvider
from .conference import extract_conference_url


READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"


class GoogleCalendarProvider(CalendarProvider):
    """Read-only Google Calendar API backend using desktop OAuth."""

    name = "google"

    def __init__(
        self,
        *,
        credentials_path: Path,
        token_path: Path,
        calendar_ids: list[str] | None = None,
    ):
        self.credentials_path = credentials_path.expanduser()
        self.token_path = token_path.expanduser()
        self.calendar_ids = calendar_ids or ["primary"]

    @staticmethod
    def _imports():
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Google Calendar support requires:\n"
                "  google-api-python-client\n"
                "  google-auth-httplib2\n"
                "  google-auth-oauthlib\n\n"
                "With uv, add them to the project with:\n"
                "  uv add google-api-python-client "
                "google-auth-httplib2 google-auth-oauthlib"
            ) from exc

        return Request, Credentials, InstalledAppFlow, build

    def _credentials(self, *, interactive: bool):
        Request, Credentials, InstalledAppFlow, _ = self._imports()
        creds = None

        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self.token_path),
                [READONLY_SCOPE],
            )

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())

        if not creds or not creds.valid:
            if not interactive:
                raise RuntimeError(
                    "Google Calendar is not authenticated. Run:\n"
                    "  ctx calendar auth"
                )

            if not self.credentials_path.exists():
                raise RuntimeError(
                    "Google OAuth desktop credentials file not found:\n"
                    f"  {self.credentials_path}\n\n"
                    "Create a Desktop app OAuth client in Google Cloud, "
                    "download its JSON credentials, and place them there."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_path),
                [READONLY_SCOPE],
            )
            creds = flow.run_local_server(port=0)

        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        return creds

    def authenticate(self) -> None:
        self._credentials(interactive=True)


    def _conference_url(self, event: dict[str, Any]) -> str | None:
        preferred: list[str] = []

        if event.get("hangoutLink"):
            preferred.append(str(event["hangoutLink"]))

        conference_data = event.get("conferenceData") or {}
        for entry_point in conference_data.get("entryPoints", []) or []:
            if entry_point.get("entryPointType") == "video" and entry_point.get("uri"):
                preferred.append(str(entry_point["uri"]))

        return extract_conference_url(
            event.get("description") or "",
            preferred_urls=preferred,
        )

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        _, _, _, build = self._imports()
        creds = self._credentials(interactive=False)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        items: list[CalendarEvent] = []

        for calendar_id in self.calendar_ids:
            page_token = None

            while True:
                response = (
                    service.events()
                    .list(
                        calendarId=calendar_id,
                        timeMin=start.isoformat(),
                        timeMax=end.isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                        showDeleted=False,
                        pageToken=page_token,
                    )
                    .execute()
                )

                for event in response.get("items", []):
                    # All-day events use `date`; ctx currently only reasons
                    # about timed meetings.
                    start_value = (event.get("start") or {}).get("dateTime")
                    end_value = (event.get("end") or {}).get("dateTime")
                    if not start_value or not end_value:
                        continue

                    attendees = [
                        attendee.get("email", "")
                        for attendee in event.get("attendees", [])
                        if attendee.get("email")
                    ]

                    items.append(
                        {
                            "id": event.get("id"),
                            "title": event.get("summary") or "",
                            "start": start_value,
                            "end": end_value,
                            "notes": event.get("description") or "",
                            "attendees": attendees,
                            "calendar": calendar_id,
                            "provider": self.name,
                            "conference_url": self._conference_url(event),
                        }
                    )

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        items.sort(key=lambda event: event["start"])
        return items

    def describe(self) -> str:
        calendars = ", ".join(self.calendar_ids)
        return f"google ({calendars})"
