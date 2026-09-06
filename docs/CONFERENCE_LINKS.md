# Calendar conference links

Opt a context in with:

```yaml
calendar:
  match:
    attendee: person@example.com
  conference: true
```

When that context is launched from `cn` (or by `ctx now`), ctx opens the
conference URL attached to that exact event. Direct launches via `co` do not.

Supported services:
- Google Meet
- Microsoft Teams
- Zoom

The Google provider prefers structured Google Calendar conference metadata and
falls back to recognised URLs in the event description. EventKit extracts
recognised URLs from event notes.

No Alfred Run Script change is required if `cn` already invokes:

    /opt/homebrew/bin/uv run ctx "$1"

`alfred-now` now places an opaque event reference in `arg`; ctx recognises it,
re-reads the event, verifies that it still resolves to the same context, and
then opens the context with that event attached.
