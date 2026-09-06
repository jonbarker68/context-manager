# Google Calendar provider setup

1. Enable the Google Calendar API in a Google Cloud project.
2. Create an OAuth 2.0 Client ID of type **Desktop app**.
3. Download the credentials JSON to:
   `~/.config/ctx/google-calendar-credentials.json`
4. Add the Python dependencies:
   `uv add google-api-python-client google-auth-httplib2 google-auth-oauthlib`
5. Copy the relevant `calendar:` section from `config.example.yaml` to:
   `~/.config/ctx/config.yaml`
6. Run:
   `ctx calendar auth`
7. Verify:
   `ctx calendar status`
   `ctx now`
   `ctx alfred-now`

The provider requests only:
`https://www.googleapis.com/auth/calendar.events.readonly`
