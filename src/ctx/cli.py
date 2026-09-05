#!/usr/bin/env python3

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

CONTEXT_DIR = Path("~/Contexts").expanduser()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CALENDAR_QUERY = PROJECT_ROOT / "native" / "calendar-query" / "calendar-query"

STATE_DIR = Path("~/.local/share/ctx").expanduser()
STATE_FILE = STATE_DIR / "spaces.json"

CTX_SPACES = [
    "ctx-1",
    "ctx-2",
    "ctx-3",
    "ctx-4",
]


def find_yabai() -> str:
    """Find yabai reliably, including when ctx is launched by Alfred."""
    found = shutil.which("yabai")
    if found:
        return found

    for candidate in (
        "/opt/homebrew/bin/yabai",
        "/usr/local/bin/yabai",
    ):
        if Path(candidate).exists():
            return candidate

    raise SystemExit("Unable to find yabai. Is the yabai service installed?")


YABAI = find_yabai()


def parse_event_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_event_time(value: str) -> str:
    dt = parse_event_time(value)
    return dt.astimezone().strftime("%H:%M")


def calendar_event_status(event: dict[str, Any], now: datetime) -> str:
    start = parse_event_time(event["start"])
    end = parse_event_time(event["end"])

    if start <= now < end:
        return "now"

    if end <= now:
        return "past"

    return "future"


def calendar_event_distance(event: dict[str, Any], now: datetime) -> float:
    """
    Distance in seconds from the event to 'now'.

    Current events have distance zero.
    Past events are measured from their end time.
    Future events are measured from their start time.
    """
    start = parse_event_time(event["start"])
    end = parse_event_time(event["end"])

    if start <= now < end:
        return 0

    if end <= now:
        return (now - end).total_seconds()

    return (start - now).total_seconds()


def read_context(context_id: str) -> dict[str, Any]:
    path = CONTEXT_DIR / f"{context_id}.md"

    if not path.exists():
        raise SystemExit(f"Unknown context: {context_id}")

    text = path.read_text()

    if not text.startswith("---"):
        raise SystemExit(f"No YAML front matter in {path}")

    parts = text.split("---", 2)

    if len(parts) < 3:
        raise SystemExit(f"Invalid front matter in {path}")

    data = yaml.safe_load(parts[1]) or {}
    data["_id"] = context_id
    data["_path"] = path

    return data


def iter_contexts():
    for path in sorted(CONTEXT_DIR.glob("*.md")):
        context_id = path.stem

        try:
            yield read_context(context_id)
        except Exception as exc:
            print(f"Warning: unable to read {path}: {exc}", file=sys.stderr)


def expand(path: str) -> str:
    return str(Path(os.path.expandvars(path)).expanduser())


def normalise_url(item: Any) -> tuple[str, str]:
    if isinstance(item, str):
        return item, item

    if isinstance(item, dict):
        url = item.get("url")
        if not url:
            raise ValueError(f"URL entry has no 'url': {item}")

        return item.get("label", url), url

    raise ValueError(f"Invalid URL entry: {item}")


def normalise_chatgpt(item: Any) -> str:
    """Return the ChatGPT project URL from a context's `chatgpt` entry.

    Supported forms:

        chatgpt: https://chatgpt.com/g/...

    or, for a more explicit/future-proof form:

        chatgpt:
          project: https://chatgpt.com/g/...

    Keeping this separate from `urls` lets us later change ChatGPT launching
    (for example to use the desktop app) without changing context files.
    """
    if isinstance(item, str):
        return item

    if isinstance(item, dict):
        project = item.get("project")
        if not project:
            raise ValueError(f"ChatGPT entry has no 'project': {item}")
        return project

    raise ValueError(f"Invalid ChatGPT entry: {item}")


def normalise_file(item: Any) -> tuple[str, str]:
    if isinstance(item, str):
        path = expand(item)
        return Path(path).name, path

    if isinstance(item, dict):
        raw_path = item.get("path")
        if not raw_path:
            raise ValueError(f"File entry has no 'path': {item}")

        path = expand(raw_path)
        return item.get("label", Path(path).name), path

    raise ValueError(f"Invalid file entry: {item}")


def find_code() -> str:
    code = shutil.which("code")

    if code:
        return code

    fallback = Path(
        "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
    )

    if fallback.exists():
        return str(fallback)

    raise SystemExit(
        "Unable to find the VS Code 'code' command. "
        "Install it in PATH or check the VS Code installation."
    )


def open_context(context_id: str) -> None:
    ctx = read_context(context_id)

    print(f"Opening context: {ctx.get('name', context_id)}")

    code = find_code()

    for path in ctx.get("vscode", []):
        subprocess.Popen(
            [
                code,
                "--new-window",
                expand(path),
            ],
            start_new_session=True,
        )

    for path in ctx.get("terminal", []):
        subprocess.Popen(
            [
                "/Applications/kitty.app/Contents/MacOS/kitty",
                "--single-instance",
                "--directory",
                expand(path),
            ],
            start_new_session=True,
        )

    # Open all context URLs together in one fresh Chrome window.
    #
    # Opening URLs one-by-one with macOS `open` lets Chrome decide how to
    # reuse/recreate windows. In particular, when Chrome has no open windows it
    # can revive a previously closed window and then add the new URLs to it,
    # which causes context tabs to accumulate across open/close cycles.
    #
    # Instead, treat the context file as authoritative: deduplicate its URLs
    # while preserving order, then ask Chrome for one new window containing
    # exactly those tabs.
    urls = []
    seen_urls = set()

    for item in ctx.get("urls", []):
        _, url = normalise_url(item)

        if url not in seen_urls:
            urls.append(url)
            seen_urls.add(url)

    # ChatGPT is deliberately a separate context resource rather than just
    # another generic URL. For now its project URL is opened as another tab in
    # the context's Chrome window. Later this can be redirected to the ChatGPT
    # desktop app without requiring context-file changes.
    chatgpt = ctx.get("chatgpt")
    if chatgpt:
        chatgpt_url = normalise_chatgpt(chatgpt)
        if chatgpt_url not in seen_urls:
            urls.append(chatgpt_url)
            seen_urls.add(chatgpt_url)

    if urls:
        chrome = Path(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )

        if not chrome.exists():
            raise SystemExit(
                "Google Chrome is required for context URLs but was not found "
                "in /Applications."
            )

        subprocess.Popen(
            [
                str(chrome),
                "--new-window",
                *urls,
            ],
            start_new_session=True,
        )

    for item in ctx.get("files", []):
        _, path = normalise_file(item)

        subprocess.Popen(
            ["open", path],
            start_new_session=True,
        )


def get_current_calendar_events() -> list[dict[str, Any]]:
    if not CALENDAR_QUERY.exists():
        raise SystemExit(
            f"Calendar helper not found:\n"
            f"  {CALENDAR_QUERY}\n\n"
            "Build it before using 'ctx now'."
        )

    result = subprocess.run(
        [str(CALENDAR_QUERY)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        message = result.stderr.strip() or "Unknown error"

        raise SystemExit(f"Unable to query Calendar:\n{message}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Calendar helper returned invalid JSON:\n{result.stdout}\n\n{exc}"
        )


def extract_context_id(text: str) -> str | None:
    match = re.search(
        r"(?im)\bctx\s*:\s*([A-Za-z0-9_.-]+)",
        text,
    )

    if match:
        return match.group(1)

    return None


def show_now() -> None:
    events = get_current_calendar_events()

    tagged_events = []

    for event in events:
        notes = event.get("notes") or ""
        context_id = extract_context_id(notes)

        if context_id:
            tagged_events.append((event, context_id))

    if not tagged_events:
        print("No current calendar event has a context tag.")
        return

    if len(tagged_events) > 1:
        print("Multiple current calendar events have context tags:")
        for event, context_id in tagged_events:
            print(f"  {event.get('title', '(untitled)')}: {context_id}")
        return

    event, context_id = tagged_events[0]

    print(
        f"Opening context '{context_id}' "
        f"for calendar event '{event.get('title', '(untitled)')}'"
    )

    activate_context(context_id)


# ----------------------------------------------------------------------
# Yabai/State layer
# ----------------------------------------------------------------------


def yabai(*args: str) -> Any:
    result = subprocess.run(
        [YABAI, "-m", *args],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or f"yabai command failed: {' '.join(args)}"
        )

    output = result.stdout.strip()

    if not output:
        return None

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def load_space_state() -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_space_state(state: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def get_current_space() -> dict[str, Any]:
    return yabai("query", "--spaces", "--space")


def focus_space(label: str) -> None:
    yabai("space", "--focus", label)

    # Give macOS's Space transition a moment to finish before
    # opening applications/windows.
    time.sleep(0.5)


def windows_on_current_space() -> list[dict[str, Any]]:
    return yabai("query", "--windows", "--space") or []


def windows_on_space(label: str) -> list[dict[str, Any]]:
    return yabai("query", "--windows", "--space", label) or []


def all_windows() -> list[dict[str, Any]]:
    """Return all yabai-visible windows."""
    return yabai("query", "--windows") or []


def ensure_new_windows_on_space(
    target_space: str,
    existing_window_ids: set[int],
    timeout: float = 3.0,
    poll_interval: float = 0.25,
) -> None:
    """Move windows created by this context launch onto its allocated Space.

    Some applications choose a display/Space independently of the currently
    focused Space (Acrobat is one example).  Snapshot the window ids before
    launching the context, then for a short bounded period watch for newly
    created windows.  Any new window that appears outside target_space is
    moved there with yabai.

    This is deliberately best-effort: failure to move one window is reported
    but does not abort the whole context launch.
    """
    deadline = time.monotonic() + timeout
    handled: set[int] = set()

    while time.monotonic() < deadline:
        windows = all_windows()
        target_ids = {
            window.get("id")
            for window in windows_on_space(target_space)
            if window.get("id") is not None
        }

        for window in windows:
            window_id = window.get("id")

            if (
                window_id is None
                or window_id in existing_window_ids
                or window_id in handled
            ):
                continue

            handled.add(window_id)

            if window_id in target_ids:
                continue

            try:
                yabai("window", str(window_id), "--space", target_space)
            except RuntimeError as exc:
                print(
                    f"Warning: could not move new "
                    f"{window.get('app', '?')} window to {target_space}: {exc}",
                    file=sys.stderr,
                )

        time.sleep(poll_interval)


def reconcile_space_topology() -> None:
    """
    Ensure ctx Space labels match the current display topology.

    Expected layouts:

    One display:
        main, ctx-1, ctx-2, ctx-3, ctx-4

    Two displays:
        display 1: auxiliary
        display 2: main, ctx-1, ctx-2, ctx-3, ctx-4

    macOS moves the four persistent context Spaces between displays when
    an external display is connected/disconnected. We therefore only need
    to repair their semantic labels.
    """
    spaces = yabai("query", "--spaces") or []
    displays = yabai("query", "--displays") or []

    if len(displays) == 1:
        display_spaces = sorted(
            (
                space
                for space in spaces
                if space.get("display") == displays[0].get("index")
            ),
            key=lambda space: space["index"],
        )

        if len(display_spaces) != 5:
            raise SystemExit(
                "Unexpected Space layout for one display: "
                f"expected 5 Spaces, found {len(display_spaces)}.\n"
                "Refusing to relabel Spaces automatically."
            )

        desired_labels = [
            "main",
            "ctx-1",
            "ctx-2",
            "ctx-3",
            "ctx-4",
        ]

        for space, desired_label in zip(display_spaces, desired_labels):
            if space.get("label") != desired_label:
                yabai(
                    "space",
                    str(space["index"]),
                    "--label",
                    desired_label,
                )

        return

    if len(displays) == 2:
        # The built-in laptop display is display 1 in the topology we have
        # observed, and the external monitor is display 2.
        #
        # More importantly, our desired topology is unambiguous:
        # one display has exactly one Space (auxiliary), while the other
        # has exactly five Spaces (main + four context Spaces).

        spaces_by_display: dict[int, list[dict[str, Any]]] = {}

        for space in spaces:
            display_index = space.get("display")
            spaces_by_display.setdefault(display_index, []).append(space)

        for display_spaces in spaces_by_display.values():
            display_spaces.sort(key=lambda space: space["index"])

        auxiliary_candidates = [
            display_spaces
            for display_spaces in spaces_by_display.values()
            if len(display_spaces) == 1
        ]

        context_candidates = [
            display_spaces
            for display_spaces in spaces_by_display.values()
            if len(display_spaces) == 5
        ]

        if len(auxiliary_candidates) != 1 or len(context_candidates) != 1:
            counts = sorted(
                len(display_spaces) for display_spaces in spaces_by_display.values()
            )

            raise SystemExit(
                "Unexpected Space layout for two displays: "
                f"found {counts} Spaces per display; expected [1, 5].\n"
                "Refusing to relabel Spaces automatically."
            )

        auxiliary_space = auxiliary_candidates[0][0]
        context_spaces = context_candidates[0]

        if auxiliary_space.get("label") != "auxiliary":
            yabai(
                "space",
                str(auxiliary_space["index"]),
                "--label",
                "auxiliary",
            )

        desired_labels = [
            "main",
            "ctx-1",
            "ctx-2",
            "ctx-3",
            "ctx-4",
        ]

        for space, desired_label in zip(context_spaces, desired_labels):
            if space.get("label") != desired_label:
                yabai(
                    "space",
                    str(space["index"]),
                    "--label",
                    desired_label,
                )

        return

    raise SystemExit(
        f"ctx currently supports one or two displays; found {len(displays)}."
    )


def reconcile_space_state() -> dict[str, str]:
    """Reconcile cached context assignments with the actual Space contents.

    spaces.json records which context a managed Space belongs to, which cannot be
    reconstructed from yabai alone.  Yabai is used to validate the physical state:
    an assignment with no windows is stale and is removed.
    """
    state = load_space_state()
    changed = False

    for space, context_id in list(state.items()):
        # Ignore any obsolete/non-managed entries rather than using them for
        # allocation decisions.
        if space not in CTX_SPACES:
            del state[space]
            changed = True
            continue

        if not windows_on_space(space):
            print(
                f"Removing stale assignment: {space} -> {context_id}",
                file=sys.stderr,
            )
            del state[space]
            changed = True

    if changed:
        save_space_state(state)

    return state


def window_exists(window_id: int) -> bool:
    """Return True if yabai can still see the given window."""
    windows = yabai("query", "--windows") or []
    return any(window.get("id") == window_id for window in windows)


def _applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def app_window_count_on_space(space_label: str, app_name: str) -> int:
    """Return the number of yabai-visible windows for an app on one Space."""
    return sum(
        1
        for window in windows_on_space(space_label)
        if window.get("app") == app_name
    )


def close_window_via_accessibility(
    window: dict[str, Any],
    space_label: str,
) -> bool:
    """Close one app window via macOS Accessibility.

    Some applications (notably VS Code in some states, and Acrobat) expose
    windows that yabai can enumerate but cannot address by window id.  Do not
    try to focus the yabai id here: that is precisely the operation that may
    fail.  Instead, while still on the context Space, make the application
    frontmost and close its front window through System Events.

    Success is verified by checking that the number of that application's
    windows on the context Space decreases.
    """
    app_name = window.get("app") or ""

    if not app_name:
        return False

    before = app_window_count_on_space(space_label, app_name)
    if before == 0:
        # The captured window has already disappeared.
        return True

    app = _applescript_string(app_name)

    script = f'''
    tell application "System Events"
        tell process "{app}"
            set frontmost to true
            try
                perform action "AXPress" of button 1 of front window
            on error
                keystroke "w" using command down
            end try
        end tell
    end tell
    '''

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        message = result.stderr.strip() or "unknown Accessibility error"
        print(
            f"Accessibility close failed for {app_name}: {message}",
            file=sys.stderr,
        )
        return False

    # Give the application/yabai a moment to observe the closed window.
    for _ in range(5):
        time.sleep(0.1)
        after = app_window_count_on_space(space_label, app_name)
        if after < before:
            return True

    return False


def close_window(window: dict[str, Any], space_label: str) -> bool:
    """Close one macOS window without quitting its application."""
    window_id = window.get("id")

    if window_id is None:
        return False

    try:
        yabai("window", str(window_id), "--close")
        time.sleep(0.15)
        if not window_exists(window_id):
            return True
    except RuntimeError:
        pass

    return close_window_via_accessibility(window, space_label)


# ----------------------------------------------------------------------
# Listing / Alfred
# ----------------------------------------------------------------------


def list_contexts() -> None:
    for ctx in iter_contexts():
        context_id = ctx["_id"]
        name = ctx.get("name", context_id)
        description = ctx.get("description", "")

        print(f"{context_id:30} {name:30} {description}")


def alfred_open_contexts() -> dict[str, str]:
    """
    Return a context_id -> Space mapping for Alfred display purposes.

    This deliberately reads spaces.json without reconciling it against yabai:
    Alfred may invoke the Script Filter repeatedly while the user types, so we
    keep this path cheap and side-effect free. Context activation performs the
    authoritative reconciliation before acting.
    """
    state = load_space_state()
    return {
        context_id: space
        for space, context_id in state.items()
        if space in CTX_SPACES
    }


def alfred_calendar_items(
    open_contexts: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    open_contexts = open_contexts or {}

    candidates = []

    for event in get_current_calendar_events():
        notes = event.get("notes") or ""
        context_id = extract_context_id(notes)

        if not context_id:
            continue

        context_file = CONTEXT_DIR / f"{context_id}.md"

        if not context_file.exists():
            continue

        try:
            ctx = read_context(context_id)
        except SystemExit:
            continue

        status = calendar_event_status(event, now)

        candidates.append(
            {
                "event": event,
                "context_id": context_id,
                "context": ctx,
                "status": status,
                "distance": calendar_event_distance(event, now),
            }
        )

    # Current event first; everything else by proximity to now.
    candidates.sort(
        key=lambda item: (
            0 if item["status"] == "now" else 1,
            item["distance"],
        )
    )

    # Identify the nearest future event so we can call it NEXT.
    future_items = [item for item in candidates if item["status"] == "future"]

    next_item = future_items[0] if future_items else None

    items = []

    for item in candidates:
        event = item["event"]
        context_id = item["context_id"]
        ctx = item["context"]
        status = item["status"]

        event_title = event.get("title") or "(untitled event)"
        context_name = ctx.get("name", context_id)
        event_time = format_event_time(event["start"])

        if status == "now":
            title = f"NOW — {event_time} {event_title}"
        elif item is next_item:
            title = f"NEXT — {event_time} {event_title}"
        elif status == "past":
            title = f"EARLIER — {event_time} {event_title}"
        else:
            title = f"LATER — {event_time} {event_title}"

        open_space = open_contexts.get(context_id)
        subtitle = context_name
        if open_space:
            subtitle = f"OPEN — {open_space} · {context_name}"

        items.append(
            {
                "uid": (
                    f"calendar:{event.get('start', '')}:{context_id}:{event_title}"
                ),
                "title": title,
                "subtitle": subtitle,
                "arg": context_id,
                "valid": True,
                "match": " ".join(
                    [
                        event_title,
                        context_name,
                        context_id,
                        event_time,
                        status,
                    ]
                ).lower(),
            }
        )

    return items


def alfred_contexts(query: str = "") -> None:
    query = query.strip().lower()
    open_contexts = alfred_open_contexts()

    items = []

    try:
        items.extend(alfred_calendar_items(open_contexts))
    except Exception as exc:
        print(
            f"Warning: unable to read calendar contexts: {exc}",
            file=sys.stderr,
        )

    # Normal context registry follows.
    for ctx in iter_contexts():
        context_id = ctx["_id"]
        name = ctx.get("name", context_id)
        description = ctx.get("description", "")

        searchable = " ".join(
            [
                context_id,
                name,
                description,
            ]
        ).lower()

        if query and query not in searchable:
            continue

        open_space = open_contexts.get(context_id)
        subtitle = description or context_id
        if open_space:
            subtitle = f"OPEN — {open_space} · {subtitle}"

        items.append(
            {
                "uid": context_id,
                "title": name,
                "subtitle": subtitle,
                "arg": context_id,
                "autocomplete": context_id,
                "match": searchable,
                "valid": True,
            }
        )

    print(
        json.dumps(
            {
                "skipknowledge": True,
                "items": items,
            }
        )
    )


def alfred_close_contexts(query: str = "") -> None:
    """Emit Alfred Script Filter JSON for currently open contexts only.

    When Alfred is invoked from a managed context Space, put that context first
    so bare ``cc`` + Return remains the fast "close the context I'm in" action.
    """
    query = query.strip().lower()
    open_contexts = alfred_open_contexts()

    try:
        current_space = get_current_space()
        current_space_label = current_space.get("label", "")
    except Exception:
        # Alfred listing should remain useful even if yabai has a transient
        # query failure. In that case we simply omit CURRENT prioritisation.
        current_space_label = ""

    current_context_id = next(
        (
            context_id
            for context_id, space in open_contexts.items()
            if space == current_space_label
        ),
        None,
    )

    items = []

    for ctx in iter_contexts():
        context_id = ctx["_id"]
        space = open_contexts.get(context_id)
        if not space:
            continue

        name = ctx.get("name", context_id)
        description = ctx.get("description", "")
        searchable = " ".join([context_id, name, description, space]).lower()

        if query and query not in searchable:
            continue

        is_current = context_id == current_context_id

        subtitle = f"CURRENT — {space}" if is_current else f"OPEN — {space}"
        if description:
            subtitle += f" · {description}"

        items.append(
            {
                "uid": f"close:{context_id}",
                "title": name,
                "subtitle": subtitle,
                "arg": context_id,
                "autocomplete": context_id,
                "match": searchable,
                "valid": True,
                "_is_current": is_current,
            }
        )

    # Current context always wins the default Alfred selection.  Remaining
    # contexts retain their existing order.
    items.sort(key=lambda item: 0 if item["_is_current"] else 1)

    for item in items:
        item.pop("_is_current", None)

    print(json.dumps({"skipknowledge": True, "items": items}))


def activate_context(context_id: str) -> None:
    # Validate the context before allocating a Space.
    context_file = CONTEXT_DIR / f"{context_id}.md"

    if not context_file.exists():
        raise SystemExit(f"Unknown context: {context_id}")

    # First repair semantic Space labels following any display change.
    reconcile_space_topology()

    state = reconcile_space_state()

    # Is this context already open? Verify that the recorded Space still
    # contains windows; repair stale state left by an interrupted close.
    for space, active_context in list(state.items()):
        if active_context != context_id:
            continue

        if windows_on_space(space):
            focus_space(space)
            print(f"Switched to {context_id} on {space}")
            return

        state.pop(space, None)
        save_space_state(state)

    # Otherwise allocate a genuinely empty context Space.  A Space that is not
    # in spaces.json but still contains windows is occupied/unknown and must not
    # be reused.
    free_space = next(
        (
            space
            for space in CTX_SPACES
            if space not in state and not windows_on_space(space)
        ),
        None,
    )

    if free_space is None:
        raise SystemExit(
            "No free context Spaces.\n"
            "Close an existing context with 'ctx close', or clear windows from an "
            "unmanaged context Space."
        )

    focus_space(free_space)

    # Snapshot existing windows before launching. Some applications (notably
    # Acrobat) may create their new window on another display/Space even though
    # free_space is focused. We will move only windows that appear after this
    # snapshot, leaving all pre-existing windows untouched.
    existing_window_ids = {
        window.get("id")
        for window in all_windows()
        if window.get("id") is not None
    }

    # Record allocation before launching, so a partially failed
    # launch doesn't accidentally allow this Space to be reused.
    state[free_space] = context_id
    save_space_state(state)

    try:
        open_context(context_id)
        ensure_new_windows_on_space(free_space, existing_window_ids)
    except Exception:
        state.pop(free_space, None)
        save_space_state(state)
        raise

    print(f"Opened {context_id} on {free_space}")


def close_context(context_id: str | None = None) -> None:
    """Close a managed context.

    With no context_id, close the context on the current Space and return to
    main (the historical `ctx close` behaviour). With a context_id, locate its
    assigned Space, close it, and return to the Space that was active when the
    command started.
    """
    reconcile_space_topology()

    original_space = get_current_space()
    original_label = original_space.get("label", "")
    state = load_space_state()

    if context_id is None:
        space_label = original_label
        if space_label not in CTX_SPACES:
            raise SystemExit(
                "Current Space is not a managed context Space; "
                "refusing to close its windows."
            )

        context_id = state.get(space_label)
        if not context_id:
            raise SystemExit(f"{space_label} is not currently assigned to a context.")

        return_label = "main"
    else:
        # Validate the named context so typos fail clearly.
        context_file = CONTEXT_DIR / f"{context_id}.md"
        if not context_file.exists():
            raise SystemExit(f"Unknown context: {context_id}")

        space_label = next(
            (space for space, active_context in state.items() if active_context == context_id),
            None,
        )
        if not space_label:
            raise SystemExit(f"Context '{context_id}' is not currently open.")

        return_label = original_label or "main"

    # Capture the target Space's windows before changing focus. Accessibility
    # fallback requires that Space to be active, so focus it briefly if needed.
    windows = windows_on_space(space_label)
    if original_label != space_label:
        focus_space(space_label)

    # Release the allocation BEFORE closing anything. A terminal window on the
    # target Space may otherwise terminate a command launched from that Space.
    state.pop(space_label, None)
    save_space_state(state)

    print(f"Released {context_id} from {space_label}")

    terminal_apps = {"kitty", "Terminal", "iTerm2"}
    ordinary_windows = [
        window for window in windows if window.get("app") not in terminal_apps
    ]
    terminal_windows = [
        window for window in windows if window.get("app") in terminal_apps
    ]

    failures = []
    for window in ordinary_windows:
        if not close_window(window, space_label):
            failures.append(window)

    # Return before closing terminal windows. For a remote close this restores
    # the user's original workspace; for bare `ctx close` it preserves the
    # established behaviour of returning to main.
    try:
        yabai("space", "--focus", return_label)
        time.sleep(0.5)
    except RuntimeError:
        # If a label disappeared during a display change, main is the safest
        # fallback.
        if return_label != "main":
            yabai("space", "--focus", "main")
            time.sleep(0.5)

    for window in terminal_windows:
        window_id = window.get("id")
        if window_id is None:
            continue
        try:
            yabai("window", str(window_id), "--close")
        except RuntimeError:
            failures.append(window)

    if failures:
        print("Some context windows could not be closed:", file=sys.stderr)
        for window in failures:
            print(
                f"  {window.get('app', '?')} — {window.get('title', '')}",
                file=sys.stderr,
            )


def close_current_context() -> None:
    """Backward-compatible wrapper for the original no-argument close."""
    close_context()


def usage() -> None:
    print(
        """Usage:

  ctx list
      List all contexts.

  ctx now
      Show the current Calendar event and associated context.

  ctx alfred [query]
      Emit Alfred Script Filter JSON for opening/switching contexts.

  ctx alfred-close [query]
      Emit Alfred Script Filter JSON for currently open contexts.

  ctx close [context-id]
      With no context id, close the context on the current managed Space and
      return to main. With a context id, close that context wherever it is
      open and return to the previously active Space.

  ctx <context-id>
      Open or switch to a context.

Examples:

  ctx list
  ctx now
  ctx teaching-com413
  ctx close
  ctx close teaching-com413
  ctx alfred com
  ctx alfred-close com
"""
    )


def main() -> None:
    if len(sys.argv) < 2:
        list_contexts()
        return

    command = sys.argv[1]

    if command in {"-h", "--help", "help"}:
        usage()
        return

    if command == "list":
        list_contexts()
        return

    if command == "now":
        show_now()
        return

    if command == "alfred":
        query = " ".join(sys.argv[2:])
        alfred_contexts(query)
        return

    if command == "alfred-close":
        query = " ".join(sys.argv[2:])
        alfred_close_contexts(query)
        return

    if command == "close":
        context_id = sys.argv[2] if len(sys.argv) > 2 else None
        close_context(context_id)
        return

    return activate_context(command)


if __name__ == "__main__":
    main()
