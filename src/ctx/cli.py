#!/usr/bin/env python3

import difflib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    from .calendar_providers import (
        DEFAULT_CONFIG_PATH,
        calendar_events_for_days,
        get_calendar_provider,
    )
except ImportError:
    # Allows direct execution of cli.py during development.
    from calendar_providers import (
        DEFAULT_CONFIG_PATH,
        calendar_events_for_days,
        get_calendar_provider,
    )

CONTEXT_CONFIG_FILE = Path("~/.config/ctx/config.yaml").expanduser()

DEFAULT_CONTEXTS_CONFIG = {
    "root": "~/shared/notes/obsidian/contexts",
}

DEFAULT_NOTES_CONFIG = {
    "root": "~/shared/notes/obsidian",
    "extension": ".md",
    "vscode_profile": "Foam Notes",
    "default": "index.md",
}

DEFAULT_TODO_CONFIG = {
    "file": "todo.md",
    "section": "Inbox",
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path("~/.local/share/ctx").expanduser()
STATE_FILE = STATE_DIR / "spaces.json"

ALFRED_CALENDAR_DAYS = 4  # today + next three days

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


def _read_context_path(path: Path) -> dict[str, Any]:
    """Read one context descriptor from Markdown/YAML front matter."""
    text = path.read_text()

    if not text.startswith("---"):
        raise SystemExit(f"No YAML front matter in {path}")

    parts = text.split("---", 2)

    if len(parts) < 3:
        raise SystemExit(f"Invalid front matter in {path}")

    data = yaml.safe_load(parts[1]) or {}
    stable_id = data.get("id")

    # Legacy descriptors pre-date immutable ids. They remain readable so the
    # migration can be incremental, but directory markers are only written for
    # contexts that have a real id.
    data["_id"] = stable_id or path.stem
    data["_stable_id"] = stable_id
    data["_file_key"] = path.stem
    data["_path"] = path

    aliases = data.get("aliases", []) or []
    if isinstance(aliases, str):
        aliases = [aliases]
    data["_aliases"] = [str(alias) for alias in aliases]

    return data


def iter_contexts():
    # rglob is deliberately used here: filenames and directory hierarchy are
    # storage details, not context identity.
    for path in sorted(context_dir().rglob("*.md")):
        try:
            yield _read_context_path(path)
        except Exception as exc:
            print(f"Warning: unable to read {path}: {exc}", file=sys.stderr)


def _context_terms(ctx: dict[str, Any]) -> list[str]:
    terms = [
        str(ctx.get("_id", "")),
        str(ctx.get("_file_key", "")),
        str(ctx.get("name", "")),
        *ctx.get("_aliases", []),
    ]
    return [term for term in terms if term]


def resolve_context(query: str, *, fuzzy: bool = True) -> dict[str, Any]:
    """Resolve a user-facing context reference to one descriptor.

    Resolution order is intentionally conservative:
      1. exact id / filename / name / alias (case-insensitive)
      2. unique substring match
      3. unique close fuzzy match

    The immutable id is therefore never something the user needs to type.
    """
    query = query.strip()
    if not query:
        raise SystemExit("Context name cannot be empty.")

    contexts = list(iter_contexts())
    q = query.casefold()

    exact = [
        ctx
        for ctx in contexts
        if any(term.casefold() == q for term in _context_terms(ctx))
    ]

    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        _raise_ambiguous_context(query, exact)

    if not fuzzy:
        raise SystemExit(f"Unknown context: {query}")

    substring = [
        ctx
        for ctx in contexts
        if any(q in term.casefold() for term in _context_terms(ctx))
    ]

    if len(substring) == 1:
        return substring[0]
    if len(substring) > 1:
        _raise_ambiguous_context(query, substring)

    scored: list[tuple[float, dict[str, Any]]] = []
    for ctx in contexts:
        score = max(
            (
                difflib.SequenceMatcher(None, q, term.casefold()).ratio()
                for term in _context_terms(ctx)
            ),
            default=0.0,
        )
        if score >= 0.6:
            scored.append((score, ctx))

    scored.sort(key=lambda item: item[0], reverse=True)
    if scored:
        best_score = scored[0][0]
        best = [ctx for score, ctx in scored if best_score - score < 0.08]
        if len(best) == 1:
            return best[0]
        _raise_ambiguous_context(query, best)

    raise SystemExit(f"Unknown context: {query}")


def _raise_ambiguous_context(query: str, contexts: list[dict[str, Any]]) -> None:
    lines = [f"Ambiguous context '{query}'. Matches:"]
    for ctx in contexts[:10]:
        name = ctx.get("name", ctx.get("_file_key", "(unnamed)"))
        aliases = ctx.get("_aliases", [])
        suffix = f" (aliases: {', '.join(aliases)})" if aliases else ""
        lines.append(f"  {name}{suffix}")
    raise SystemExit("\n".join(lines))


def read_context(context_ref: str) -> dict[str, Any]:
    """Backward-compatible reader accepting id, filename, name, or alias."""
    return resolve_context(context_ref)


def context_id_fn(ctx: dict[str, Any]) -> str:
    return str(ctx["_id"])


def require_stable_id(ctx: dict[str, Any]) -> str:
    stable_id = ctx.get("_stable_id")
    if not stable_id:
        name = ctx.get("name", ctx.get("_file_key", "(unnamed)"))
        raise SystemExit(
            f"Context '{name}' has no immutable id yet.\n"
            "Run 'ctx ensure-ids' once to add ids to legacy context files."
        )
    return str(stable_id)


def generate_context_id(existing: set[str]) -> str:
    while True:
        candidate = secrets.token_hex(6)
        if candidate not in existing:
            return candidate


def slugify_context_identity(value: str) -> str:
    """Return a predictable lowercase, hyphen-separated context identity."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise SystemExit(f"Unable to derive a context name from: {value!r}")
    return slug


def display_path(path: Path) -> str:
    """Prefer ~/... for paths under the user's home directory."""
    path = path.resolve()
    home = Path.home().resolve()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def new_code_context(description: str | None = None) -> None:
    """Create a minimal code context for the current working directory."""
    project_dir = Path.cwd().resolve()
    identity = slugify_context_identity(project_dir.name)
    name = f"code-{identity}"

    context_dir().mkdir(parents=True, exist_ok=True)
    path = context_dir() / f"{name}.md"

    if path.exists():
        raise SystemExit(f"Context already exists: {path}")

    for ctx in iter_contexts():
        if str(ctx.get("name", "")).casefold() == name.casefold():
            raise SystemExit(
                f"A context named '{name}' already exists at {ctx['_path']}"
            )

    if description is None:
        try:
            description = input(f"Description for {name}: ").strip()
        except EOFError:
            raise SystemExit("A description is required.")
    else:
        description = description.strip()

    if not description:
        raise SystemExit("Description cannot be empty.")

    existing_ids = {
        str(ctx["_stable_id"]) for ctx in iter_contexts() if ctx.get("_stable_id")
    }
    context_id = generate_context_id(existing_ids)
    project_path = display_path(project_dir)

    front_matter = {
        "id": context_id,
        "name": name,
        "description": description,
        "vscode": [project_path],
        "terminal": [project_path],
    }
    yaml_text = yaml.safe_dump(
        front_matter,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    path.write_text(f"---\n{yaml_text}---\n")

    print(f"Created {path}")
    print(f"  name:     {name}")
    print(f"  vscode:   {project_path}")
    print(f"  terminal: {project_path}")


def ensure_context_ids() -> None:
    """Add an immutable random id to any legacy context descriptor."""
    contexts = list(iter_contexts())
    existing = {str(ctx["_stable_id"]) for ctx in contexts if ctx.get("_stable_id")}
    changed = 0

    for ctx in contexts:
        if ctx.get("_stable_id"):
            continue

        path = ctx["_path"]
        text = path.read_text()
        parts = text.split("---", 2)
        new_id = generate_context_id(existing)
        existing.add(new_id)

        # Insert the id without serialising the YAML again. Context descriptors
        # are human-maintained files, so comments, quoting and formatting must
        # survive migration untouched.
        front_matter = parts[1]
        if front_matter.startswith("\n"):
            front_matter = f"\nid: {new_id}" + front_matter
        else:
            front_matter = f"\nid: {new_id}\n" + front_matter
        path.write_text(f"---{front_matter}---{parts[2]}")
        print(f"{ctx.get('name', path.stem)}: {new_id}")
        changed += 1

    if changed == 0:
        print("All contexts already have immutable ids.")
    else:
        print(f"Added immutable ids to {changed} context(s).")


def edit_context(context_ref: str) -> None:
    ctx = resolve_context(context_ref)
    subprocess.Popen(
        [find_code(), str(ctx["_path"])],
        start_new_session=True,
    )
    print(f"Editing {ctx.get('name', ctx['_file_key'])}: {ctx['_path']}")


def find_context_marker(start: Path | None = None) -> Path | None:
    directory = (start or Path.cwd()).resolve()
    for candidate_dir in (directory, *directory.parents):
        marker = candidate_dir / ".ctx"
        if marker.is_file():
            return marker
    return None


def read_context_marker(marker: Path) -> str:
    text = marker.read_text().strip()
    if not text:
        raise SystemExit(f"Empty context marker: {marker}")

    # Current format is tiny YAML, but accept a bare id as a convenience.
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid context marker {marker}: {exc}")

    if isinstance(data, str):
        return data
    if isinstance(data, dict) and data.get("context"):
        return str(data["context"])

    raise SystemExit(f"Invalid context marker {marker}: expected 'context: <id>'.")


def mark_context(context_ref: str) -> None:
    ctx = resolve_context(context_ref)
    stable_id = require_stable_id(ctx)
    marker = Path.cwd() / ".ctx"

    if marker.exists():
        old_ref = read_context_marker(marker)
        try:
            old_ctx = resolve_context(old_ref, fuzzy=False)
            old_name = old_ctx.get("name", old_ref)
        except SystemExit:
            old_name = old_ref
        raise SystemExit(
            f"{marker} already exists and points to '{old_name}'.\n"
            "Remove it first if you want to change this directory's context."
        )

    marker.write_text(f"context: {stable_id}\n")
    print(f"Marked {Path.cwd()} as {ctx.get('name', ctx['_file_key'])}")


def unmark_context() -> None:
    marker = Path.cwd() / ".ctx"
    if not marker.exists():
        raise SystemExit(f"No .ctx marker in {Path.cwd()}")
    marker.unlink()
    print(f"Removed {marker}")


def activate_here() -> None:
    marker = find_context_marker()
    if marker is None:
        raise SystemExit("No .ctx marker found in this directory or any parent.")

    stable_id = read_context_marker(marker)
    ctx = resolve_context(stable_id, fuzzy=False)
    print(f"Found {ctx.get('name', ctx['_file_key'])} via {marker}")
    activate_context(context_id_fn(ctx))


def expand(path: str) -> str:
    return str(Path(os.path.expandvars(path)).expanduser())


def read_context_config() -> dict[str, Any]:
    """Read optional global ctx configuration from ~/.config/ctx/config.yaml."""
    if not CONTEXT_CONFIG_FILE.exists():
        return {}

    try:
        data = yaml.safe_load(CONTEXT_CONFIG_FILE.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"Unable to read {CONTEXT_CONFIG_FILE}: {exc}") from exc

    if not isinstance(data, dict):
        raise SystemExit(f"Invalid {CONTEXT_CONFIG_FILE}: expected a YAML mapping.")
    return data


def contexts_config() -> dict[str, str]:
    """Return resolved global settings for context descriptor storage."""
    config = read_context_config()
    configured = config.get("contexts") or {}
    if not isinstance(configured, dict):
        raise SystemExit(
            f"Invalid {CONTEXT_CONFIG_FILE}: 'contexts' must be a YAML mapping."
        )

    result = {**DEFAULT_CONTEXTS_CONFIG}
    for key in result:
        value = configured.get(key)
        if value is not None:
            result[key] = str(value)

    if not result["root"].strip():
        raise SystemExit(
            f"Invalid {CONTEXT_CONFIG_FILE}: contexts.root cannot be empty."
        )
    return result


def context_dir() -> Path:
    """Return the configured context descriptor root."""
    return Path(os.path.expandvars(contexts_config()["root"])).expanduser()


def notes_config() -> dict[str, str]:
    """Return resolved global settings for context notes."""
    config = read_context_config()
    configured = config.get("notes") or {}
    if not isinstance(configured, dict):
        raise SystemExit(
            f"Invalid {CONTEXT_CONFIG_FILE}: 'notes' must be a YAML mapping."
        )

    result = {**DEFAULT_NOTES_CONFIG}
    for key in result:
        value = configured.get(key)
        if value is not None:
            result[key] = str(value)

    extension = result["extension"].strip()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    result["extension"] = extension

    if not result["root"].strip():
        raise SystemExit(f"Invalid {CONTEXT_CONFIG_FILE}: notes.root cannot be empty.")
    if not result["vscode_profile"].strip():
        raise SystemExit(
            f"Invalid {CONTEXT_CONFIG_FILE}: notes.vscode_profile cannot be empty."
        )
    if not result["default"].strip():
        raise SystemExit(
            f"Invalid {CONTEXT_CONFIG_FILE}: notes.default cannot be empty."
        )

    return result


def todo_config() -> dict[str, str]:
    """Return resolved settings for the Markdown todo inbox."""
    config = read_context_config()
    configured = config.get("todo") or {}
    if not isinstance(configured, dict):
        raise SystemExit(
            f"Invalid {CONTEXT_CONFIG_FILE}: 'todo' must be a YAML mapping."
        )

    result = {**DEFAULT_TODO_CONFIG}
    for key in result:
        value = configured.get(key)
        if value is not None:
            result[key] = str(value)

    if not result["file"].strip():
        raise SystemExit(f"Invalid {CONTEXT_CONFIG_FILE}: todo.file cannot be empty.")
    if not result["section"].strip():
        raise SystemExit(
            f"Invalid {CONTEXT_CONFIG_FILE}: todo.section cannot be empty."
        )
    return result


def todo_path() -> Path:
    """Return the configured todo file, relative to notes.root unless absolute."""
    configured = Path(os.path.expandvars(todo_config()["file"])).expanduser()
    if configured.is_absolute():
        return configured

    notes_root = Path(os.path.expandvars(notes_config()["root"])).expanduser()
    return notes_root / configured


def _todo_timestamp(value: str | None) -> str:
    """Return a normalised todo timestamp, defaulting to local capture time."""
    if value is None:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(
            "Invalid --date. Use an ISO date/time such as '2026-09-15 09:18'."
        ) from exc

    return parsed.strftime("%Y-%m-%d %H:%M")


def add_todo(text: str, *, url: str | None = None, date: str | None = None) -> Path:
    """Prepend one task to the configured Markdown todo inbox."""
    path = todo_path()
    if not path.is_file():
        raise SystemExit(f"Todo file does not exist: {path}")

    section = todo_config()["section"].strip()
    content = path.read_text()
    lines = content.splitlines(keepends=True)

    heading_re = re.compile(rf"^##\s+{re.escape(section)}\s*$", re.IGNORECASE)
    heading_index = next(
        (i for i, line in enumerate(lines) if heading_re.match(line.rstrip("\r\n"))),
        None,
    )
    if heading_index is None:
        raise SystemExit(f"Todo section '## {section}' not found in {path}")

    task_text = text.strip()
    if not task_text:
        raise SystemExit("Todo text cannot be empty.")
    if "\n" in task_text or "\r" in task_text:
        raise SystemExit("Todo text must be a single line.")

    if url is not None:
        url = url.strip()
        if not url:
            raise SystemExit("--url cannot be empty.")
        task_text += f" ([email]({url}))"

    task = f"- [ ] {task_text} — {_todo_timestamp(date)}\n"

    # Keep the conventional blank line immediately below the heading, then
    # insert before the existing inbox items so newest captures appear first.
    insert_at = heading_index + 1
    if insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1

    lines.insert(insert_at, task)
    path.write_text("".join(lines))
    return path


def todo_command(args: list[str]) -> None:
    """Capture a task at the top of the configured todo inbox."""
    text_parts: list[str] = []
    url: str | None = None
    date: str | None = None
    i = 0

    while i < len(args):
        arg = args[i]
        if arg in {"--url", "--date"}:
            if i + 1 >= len(args):
                raise SystemExit(
                    'Usage: ctx todo <text> [--url <url>] [--date "YYYY-MM-DD HH:MM"]'
                )
            value = args[i + 1]
            if arg == "--url":
                url = value
            else:
                date = value
            i += 2
            continue
        if arg.startswith("-"):
            raise SystemExit(
                'Usage: ctx todo <text> [--url <url>] [--date "YYYY-MM-DD HH:MM"]'
            )
        text_parts.append(arg)
        i += 1

    text = " ".join(text_parts).strip()
    if not text:
        raise SystemExit(
            'Usage: ctx todo <text> [--url <url>] [--date "YYYY-MM-DD HH:MM"]'
        )

    path = add_todo(text, url=url, date=date)
    print(f"Added todo to {path}")


def normalise_notes(value: Any) -> list[str]:
    """Normalise a context's ``notes`` entry to a list of match expressions."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise SystemExit(
        "Invalid notes entry: expected a match string or a list of match strings."
    )


def _note_pattern_matches(relative_path: str, expression: str, extension: str) -> bool:
    """Return whether a root-relative note path matches a notes expression."""
    import fnmatch

    rel = relative_path.replace(os.sep, "/")
    expr = os.path.expandvars(expression.strip()).replace(os.sep, "/")
    anchored = expr.startswith("/")
    expr = expr.lstrip("/")
    if not expr:
        return False

    rel_lower = rel.casefold()
    expr_lower = expr.casefold()
    basename_lower = rel_lower.rsplit("/", 1)[-1]
    ext_lower = extension.casefold()

    expr_with_ext = (
        expr_lower
        if not extension or expr_lower.endswith(ext_lower)
        else expr_lower + ext_lower
    )
    has_sep = "/" in expr_lower
    has_wildcard = any(ch in expr_lower for ch in "*?[")

    if not has_sep:
        if has_wildcard:
            return fnmatch.fnmatchcase(basename_lower, expr_with_ext)
        stem = basename_lower
        if extension and stem.endswith(ext_lower):
            stem = stem[: -len(extension)]
        return expr_lower in stem

    if anchored:
        return fnmatch.fnmatchcase(rel_lower, expr_with_ext)

    rel_parts = rel_lower.split("/")
    expr_parts = expr_with_ext.split("/")
    if len(expr_parts) > len(rel_parts):
        return False

    width = len(expr_parts)
    return any(
        all(
            fnmatch.fnmatchcase(rel_parts[start + offset], part)
            for offset, part in enumerate(expr_parts)
        )
        for start in range(len(rel_parts) - width + 1)
    )


def resolve_note_paths(ctx: dict[str, Any], *, warn: bool = True) -> list[Path]:
    """Resolve note match expressions below notes.root, excluding contexts.root."""
    raw_notes = normalise_notes(ctx.get("notes"))
    if not raw_notes:
        return []

    config = notes_config()
    root = Path(os.path.expandvars(config["root"])).expanduser()
    extension = config["extension"]
    contexts_root = context_dir()

    if not root.is_dir():
        raise SystemExit(f"Notes root does not exist or is not a directory: {root}")

    root_resolved = root.resolve()
    contexts_resolved = contexts_root.resolve()
    exclude_contexts = (
        contexts_resolved == root_resolved or root_resolved in contexts_resolved.parents
    )

    candidates: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if extension and not path.name.casefold().endswith(extension.casefold()):
            continue
        if exclude_contexts:
            path_resolved = path.resolve()
            if (
                path_resolved == contexts_resolved
                or contexts_resolved in path_resolved.parents
            ):
                continue
        candidates.append(path)

    matched: set[Path] = set()
    unmatched: list[str] = []
    for expression in raw_notes:
        expression_matches = [
            path
            for path in candidates
            if _note_pattern_matches(
                path.relative_to(root).as_posix(), expression, extension
            )
        ]
        if expression_matches:
            matched.update(expression_matches)
        else:
            unmatched.append(expression)

    resolved = sorted(matched, key=lambda path: path.as_posix().casefold())

    if unmatched and warn:
        for expression in unmatched:
            print(
                f"Warning: no notes matched '{expression}' under {root}.",
                file=sys.stderr,
            )

    if not resolved:
        patterns = ", ".join(repr(item) for item in raw_notes)
        raise SystemExit(f"No notes matched {patterns} under {root}.")

    return resolved


def open_context_notes(ctx: dict[str, Any]) -> list[Path]:
    """Open all notes for a context in the configured VS Code profile."""
    paths = resolve_note_paths(ctx)
    if not paths:
        return []

    config = notes_config()
    root = Path(os.path.expandvars(config["root"])).expanduser()

    subprocess.Popen(
        [
            find_code(),
            "--profile",
            config["vscode_profile"],
            "--reuse-window",
            str(root),
            *(str(path) for path in paths),
        ],
        start_new_session=True,
    )
    return paths


def current_context() -> dict[str, Any]:
    """Return the context assigned to the currently focused managed Space."""
    reconcile_space_topology()
    state = reconcile_space_state()
    current_label = get_current_space().get("label", "")
    context_id = state.get(current_label)
    if not context_id:
        raise SystemExit("There is no open context on the current Space.")
    return resolve_context(context_id, fuzzy=False)


def _open_note_paths(paths: list[Path]) -> None:
    """Open note paths in the configured VS Code notes profile."""
    config = notes_config()
    root = Path(os.path.expandvars(config["root"])).expanduser()

    subprocess.Popen(
        [
            find_code(),
            "--profile",
            config["vscode_profile"],
            "--reuse-window",
            str(root),
            *(str(path) for path in paths),
        ],
        start_new_session=True,
    )


def resolve_direct_note(expression: str) -> Path:
    """Resolve one arbitrary note below notes.root, preferring exact matches."""
    config = notes_config()
    root = Path(os.path.expandvars(config["root"])).expanduser()
    extension = config["extension"]

    # Direct note opening should prefer an exact root-relative path before the
    # more permissive note-pattern matching used by context note expressions.
    # Try the expression as written, then with the configured extension.
    expr = expression.strip().lstrip("/")
    exact_candidates = [root / expr]
    if extension and not expr.casefold().endswith(extension.casefold()):
        exact_candidates.append(root / f"{expr}{extension}")

    contexts_resolved = context_dir().resolve()
    for candidate in exact_candidates:
        if not candidate.is_file():
            continue
        candidate_resolved = candidate.resolve()
        if (
            candidate_resolved == contexts_resolved
            or contexts_resolved in candidate_resolved.parents
        ):
            continue
        return candidate

    paths = resolve_note_paths({"notes": [expression]}, warn=False)

    # If there is no exact path, prefer an exact basename/stem match. This
    # makes `--open todo` select root-level todo.md rather than also matching
    # archived notes such as todo-2024.md.
    expr_name = Path(expr).name.casefold()
    expr_stem = expr_name
    if extension and expr_stem.endswith(extension.casefold()):
        expr_stem = expr_stem[: -len(extension)]
    basename_matches = [
        path
        for path in paths
        if path.name.casefold() == expr_name or path.stem.casefold() == expr_stem
    ]
    if len(basename_matches) == 1:
        return basename_matches[0]
    if basename_matches:
        paths = basename_matches

    if len(paths) > 1:
        matches = "\n".join(f"  {path.relative_to(root)}" for path in paths)
        raise SystemExit(f"Ambiguous note '{expression}'. Matches:\n{matches}")
    return paths[0]


def note_command(args: list[str]) -> None:
    """Open context notes, the configured root note, or a specific note."""
    print_only = False
    remaining = list(args)
    if "--print" in remaining:
        print_only = True
        remaining.remove("--print")

    if "--root" in remaining:
        if len(remaining) != 1:
            raise SystemExit("Usage: ctx note [--print] --root")
        path = resolve_direct_note(notes_config()["default"])
        if print_only:
            print(path)
            return
        _open_note_paths([path])
        print(f"Opened default note: {path}")
        return

    if "--open" in remaining:
        index = remaining.index("--open")
        expression_parts = remaining[index + 1 :]
        before = remaining[:index]
        if (
            before
            or not expression_parts
            or any(arg.startswith("-") for arg in expression_parts)
        ):
            raise SystemExit("Usage: ctx note [--print] --open <note>")
        expression = " ".join(expression_parts)
        path = resolve_direct_note(expression)
        if print_only:
            print(path)
            return
        _open_note_paths([path])
        print(f"Opened note: {path}")
        return

    if any(arg.startswith("-") for arg in remaining):
        raise SystemExit(
            "Usage: ctx note [--print] [name-or-alias] | "
            "ctx note [--print] --root | ctx note [--print] --open <note>"
        )

    ctx = resolve_context(" ".join(remaining)) if remaining else current_context()
    paths = resolve_note_paths(ctx)
    name = ctx.get("name", ctx.get("_file_key", ctx.get("_id", "context")))

    if not paths:
        raise SystemExit(f"Context '{name}' has no notes entry.")

    if print_only:
        for path in paths:
            print(path)
        return

    _open_note_paths(paths)
    if len(paths) == 1:
        print(f"Opened note for {name}: {paths[0]}")
    else:
        print(f"Opened {len(paths)} notes for {name}.")


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


def open_context(
    context_id: str,
    *,
    calendar_event: dict[str, Any] | None = None,
) -> None:
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

    open_context_notes(ctx)

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

    # ChatGPT is deliberately a separate context resource rather than a
    # generic browser URL. Create a fresh window in the current ChatGPT desktop
    # app via File -> New Window, then hand the project URL to that app. This
    # keeps the context window isolated from any ChatGPT windows already open
    # in other Spaces.
    chatgpt = ctx.get("chatgpt")
    if chatgpt:
        chatgpt_url = normalise_chatgpt(chatgpt)

        new_window_script = r"""
        tell application "ChatGPT"
            activate
        end tell

        delay 0.3

        tell application "System Events"
            tell process "ChatGPT"
                click menu item "New Window" of menu "File" of menu bar 1
            end tell
        end tell

        delay 0.5
        """

        result = subprocess.run(
            ["osascript", "-e", new_window_script],
            check=False,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(
                "Warning: could not create a new ChatGPT window: "
                + (result.stderr.strip() or "unknown AppleScript error"),
                file=sys.stderr,
            )

        subprocess.Popen(
            [
                "open",
                "-a",
                "ChatGPT",
                chatgpt_url,
            ],
            start_new_session=True,
        )

    # Only calendar-driven launches can supply an event, and the context must
    # opt in explicitly before an attached meeting link is opened.
    conference_url = conference_url_for_context(ctx, calendar_event)
    if conference_url and conference_url not in seen_urls:
        urls.append(conference_url)
        seen_urls.add(conference_url)

    if urls:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

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


def calendar_conference_enabled(ctx: dict[str, Any]) -> bool:
    """Return whether this context opts in to event conference links."""
    calendar = ctx.get("calendar") or {}
    if not isinstance(calendar, dict):
        return False

    value = calendar.get("conference", False)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def calendar_event_key(event: dict[str, Any]) -> str:
    """Opaque key used to carry an Alfred calendar selection back to ctx."""
    provider = str(event.get("provider") or "")
    event_id = str(event.get("id") or "")

    if event_id:
        identity = f"{provider}|id|{event_id}|{event.get('start', '')}"
    else:
        identity = "|".join(
            [
                provider,
                str(event.get("calendar") or ""),
                str(event.get("start") or ""),
                str(event.get("end") or ""),
                str(event.get("title") or ""),
            ]
        )

    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def calendar_launch_ref(context_id: str, event: dict[str, Any]) -> str:
    return f"__calendar__:{context_id}:{calendar_event_key(event)}"


def parse_calendar_launch_ref(value: str) -> tuple[str, str] | None:
    prefix = "__calendar__:"
    if not value.startswith(prefix):
        return None

    payload = value[len(prefix) :]
    context_id, separator, event_key = payload.partition(":")
    if not separator or not context_id or not event_key:
        raise SystemExit("Invalid calendar launch reference.")
    return context_id, event_key


def conference_url_for_context(
    ctx: dict[str, Any],
    event: dict[str, Any] | None,
) -> str | None:
    if event is None or not calendar_conference_enabled(ctx):
        return None

    value = str(event.get("conference_url") or "").strip()
    return value or None


def get_calendar_events(days: int = 1) -> list[dict[str, Any]]:
    """Return normalised events from the configured calendar provider."""
    try:
        provider = get_calendar_provider(project_root=PROJECT_ROOT)
        return calendar_events_for_days(provider, days)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def calendar_status() -> None:
    """Show the configured provider without querying calendar data."""
    try:
        provider = get_calendar_provider(project_root=PROJECT_ROOT)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Calendar provider: {provider.describe()}")
    print(f"Config: {DEFAULT_CONFIG_PATH}")


def calendar_auth() -> None:
    """Run provider authentication when required (Google OAuth)."""
    try:
        provider = get_calendar_provider(project_root=PROJECT_ROOT)
        provider.authenticate()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Calendar authentication ready: {provider.describe()}")


def calendar_command(args: list[str]) -> None:
    if not args or args[0] == "status":
        calendar_status()
        return

    if args[0] == "auth":
        calendar_auth()
        return

    raise SystemExit("Usage: ctx calendar [status|auth]")


def extract_context_id(text: str) -> str | None:
    match = re.search(
        r"(?im)\bctx\s*:\s*([A-Za-z0-9_.-]+)",
        text,
    )

    if match:
        return match.group(1)

    return None


def calendar_aliases(ctx: dict[str, Any]) -> list[str]:
    """Legacy Calendar title aliases.

    ``calendar.aliases`` remains supported as shorthand for a title-only
    ``any`` rule. New configurations should generally prefer ``calendar.match``.
    """
    calendar = ctx.get("calendar") or {}
    if not isinstance(calendar, dict):
        return []

    aliases = calendar.get("aliases", []) or []
    if isinstance(aliases, str):
        aliases = [aliases]

    return [str(alias).strip() for alias in aliases if str(alias).strip()]


def event_attendee_emails(event: dict[str, Any]) -> list[str]:
    """Return normalised attendee email addresses from Calendar JSON."""
    values = event.get("attendees") or []
    if isinstance(values, str):
        values = [values]

    result = []
    for value in values:
        email = str(value).strip().casefold()
        if email.startswith("mailto:"):
            email = email[7:]
        if email:
            result.append(email)

    return result


def _match_calendar_leaf(
    event: dict[str, Any],
    key: str,
    value: Any,
) -> tuple[bool, list[str]]:
    """Evaluate one Calendar matching predicate."""
    if key == "title":
        needle = str(value).strip()
        if not needle:
            return False, []
        matched = needle.casefold() in str(event.get("title") or "").casefold()
        return matched, [f"title contains '{needle}'"] if matched else []

    if key in {"attendee", "guest"}:
        needle = str(value).strip().casefold()
        if needle.startswith("mailto:"):
            needle = needle[7:]
        if not needle:
            return False, []

        attendees = event_attendee_emails(event)
        matched = needle in attendees
        return matched, [f"attendee {needle}"] if matched else []

    if key == "calendar":
        needle = str(value).strip()
        if not needle:
            return False, []
        matched = needle.casefold() in str(event.get("calendar") or "").casefold()
        return matched, [f"calendar contains '{needle}'"] if matched else []

    return False, []


def match_calendar_rule(
    event: dict[str, Any],
    rule: Any,
) -> tuple[bool, list[str]]:
    """Evaluate a recursive Calendar match expression.

    Supported forms:

        match:
          attendee: person@example.com

        match:
          any:
            - attendee: person@example.com
            - title: Thomas

        match:
          all:
            - attendee: person@example.com
            - any:
                - title: supervision
                - title: progress

    ``any`` is boolean OR; ``all`` is boolean AND. Leaf predicates currently
    support ``attendee`` (or ``guest``), ``title`` and ``calendar``.
    """
    if not isinstance(rule, dict) or not rule:
        return False, []

    if "any" in rule:
        children = rule.get("any") or []
        if not isinstance(children, list):
            children = [children]

        evidence: list[str] = []
        matched_any = False
        for child in children:
            matched, child_evidence = match_calendar_rule(event, child)
            if matched:
                matched_any = True
                evidence.extend(child_evidence)

        return matched_any, evidence

    if "all" in rule:
        children = rule.get("all") or []
        if not isinstance(children, list):
            children = [children]

        evidence: list[str] = []
        for child in children:
            matched, child_evidence = match_calendar_rule(event, child)
            if not matched:
                return False, []
            evidence.extend(child_evidence)

        return bool(children), evidence

    # Allow a compact dictionary of leaf predicates as implicit AND:
    #
    #   match:
    #     attendee: person@example.com
    #     title: supervision
    evidence: list[str] = []
    saw_leaf = False

    for key, value in rule.items():
        if key not in {"title", "attendee", "guest", "calendar"}:
            continue

        saw_leaf = True
        matched, leaf_evidence = _match_calendar_leaf(event, key, value)
        if not matched:
            return False, []
        evidence.extend(leaf_evidence)

    return saw_leaf, evidence


def calendar_match_rule(ctx: dict[str, Any]) -> Any:
    """Return a context's explicit Calendar matching rule, if present."""
    calendar = ctx.get("calendar") or {}
    if not isinstance(calendar, dict):
        return None
    return calendar.get("match")


def resolve_calendar_event(
    event: dict[str, Any],
) -> dict[str, Any]:
    """Resolve one Calendar event to a context, conservatively.

    Resolution order:
      1. explicit ``ctx:`` in event notes (authoritative override);
      2. unique match against ``calendar.match`` rules;
      3. legacy unique title match against ``calendar.aliases``;
      4. unresolved/ambiguous result -- never guess.
    """
    notes = event.get("notes") or ""
    explicit_ref = extract_context_id(notes)

    if explicit_ref:
        try:
            ctx = resolve_context(explicit_ref, fuzzy=False)
        except SystemExit:
            return {
                "status": "invalid-explicit",
                "explicit_ref": explicit_ref,
            }

        return {
            "status": "resolved",
            "context": ctx,
            "context_id": context_id_fn(ctx),
            "source": "explicit",
            "evidence": ["explicit ctx: override"],
        }

    rule_matches: list[tuple[dict[str, Any], list[str]]] = []

    for ctx in iter_contexts():
        rule = calendar_match_rule(ctx)
        if rule is None:
            continue

        matched, evidence = match_calendar_rule(event, rule)
        if matched:
            rule_matches.append((ctx, evidence))

    if len(rule_matches) == 1:
        ctx, evidence = rule_matches[0]
        return {
            "status": "resolved",
            "context": ctx,
            "context_id": context_id_fn(ctx),
            "source": "rule",
            "evidence": evidence,
        }

    if len(rule_matches) > 1:
        return {
            "status": "ambiguous",
            "source": "rule",
            "matches": [
                {
                    "context": ctx,
                    "context_id": context_id_fn(ctx),
                    "evidence": evidence,
                }
                for ctx, evidence in rule_matches
            ],
        }

    # Backward compatibility: calendar.aliases is a title-only fallback.
    title = str(event.get("title") or "")
    folded_title = title.casefold()
    alias_matches: list[tuple[dict[str, Any], list[str]]] = []

    for ctx in iter_contexts():
        matched_aliases = [
            alias for alias in calendar_aliases(ctx) if alias.casefold() in folded_title
        ]

        if matched_aliases:
            alias_matches.append((ctx, matched_aliases))

    if len(alias_matches) == 1:
        ctx, matched_aliases = alias_matches[0]
        alias = max(matched_aliases, key=len)
        return {
            "status": "resolved",
            "context": ctx,
            "context_id": context_id_fn(ctx),
            "source": "alias",
            "alias": alias,
            "evidence": [f"title contains '{alias}'"],
        }

    if len(alias_matches) > 1:
        return {
            "status": "ambiguous",
            "source": "alias",
            "matches": [
                {
                    "context": ctx,
                    "context_id": context_id_fn(ctx),
                    "aliases": matched_aliases,
                    "evidence": [
                        f"title contains '{alias}'" for alias in matched_aliases
                    ],
                }
                for ctx, matched_aliases in alias_matches
            ],
        }

    return {"status": "unresolved"}


def show_now() -> None:
    """Open the context associated with the event genuinely active now."""
    now = datetime.now(timezone.utc)
    current_events = [
        event
        for event in get_calendar_events(1)
        if calendar_event_status(event, now) == "now"
    ]

    if not current_events:
        print("No calendar event is current now.")
        return

    resolved = []
    problems = []

    for event in current_events:
        result = resolve_calendar_event(event)

        if result["status"] == "resolved":
            resolved.append((event, result))
        else:
            problems.append((event, result))

    if len(resolved) > 1:
        print("Multiple current calendar events resolve to contexts:")
        for event, result in resolved:
            context_id = result["context_id"]
            print(
                f"  {format_event_time(event['start'])} "
                f"{event.get('title', '(untitled)')}: {context_id}"
            )
        return

    if len(resolved) == 1:
        event, result = resolved[0]
        context_id = result["context_id"]
        source = result["source"]

        if source == "rule":
            detail = "calendar match rule"
        elif source == "alias":
            detail = f"calendar alias '{result['alias']}'"
        else:
            detail = "explicit ctx: override"

        print(
            f"Opening context '{context_id}' "
            f"for calendar event '{event.get('title', '(untitled)')}' "
            f"via {detail}"
        )
        activate_context(context_id, calendar_event=event)
        return

    # No current event resolved. Give a useful diagnostic for the active event(s).
    print("Current calendar event has no resolvable context:")
    for event, result in problems:
        title = event.get("title", "(untitled)")
        status = result["status"]

        if status == "ambiguous":
            names = [
                match["context"].get("name", match["context_id"])
                for match in result["matches"]
            ]
            print(f"  {title}: ambiguous ({', '.join(names)})")
        elif status == "invalid-explicit":
            print(
                f"  {title}: ctx: {result['explicit_ref']} "
                "does not name a known context"
            )
        else:
            print(f"  {title}: no ctx: override or calendar matching rule matched")


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


def meaningful_windows_on_space(label: str) -> list[dict[str, Any]]:
    """Return ordinary windows that should make a managed Space unavailable.

    Sticky/all-Spaces utility windows are deliberately ignored. They do not
    represent ownership of a context Space and should neither block allocation
    nor be offered for cleanup.
    """
    return [
        window
        for window in windows_on_space(label)
        if not window.get("is-sticky", False)
    ]


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

        # Transparently migrate live state from legacy filename-based identity
        # to the immutable id. This matters immediately after `ctx ensure-ids`.
        try:
            ctx = resolve_context(context_id, fuzzy=False)
            canonical_id = context_id_fn(ctx)
            if canonical_id != context_id:
                state[space] = canonical_id
                context_id = canonical_id
                changed = True
        except SystemExit:
            # Preserve unknown assignments while they still have windows; they
            # may refer to a descriptor temporarily unavailable to ctx.
            pass

        if not meaningful_windows_on_space(space):
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
        1 for window in windows_on_space(space_label) if window.get("app") == app_name
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
        context_id: space for space, context_id in state.items() if space in CTX_SPACES
    }


def alfred_calendar_items(
    open_contexts: dict[str, str] | None = None,
    days: int = ALFRED_CALENDAR_DAYS,
) -> list[dict[str, Any]]:
    """Return context-related Calendar events over a short look-ahead horizon.

    Today is shown first with NOW/NEXT/LATER/EARLIER semantics. Subsequent
    days are appended chronologically and preceded by disabled Alfred items
    that act as visual day separators.
    """
    now = datetime.now(timezone.utc)
    local_now = now.astimezone()
    today = local_now.date()
    open_contexts = open_contexts or {}

    candidates = []

    for event in get_calendar_events(days):
        resolution = resolve_calendar_event(event)

        if resolution["status"] == "unresolved":
            continue

        start_local = parse_event_time(event["start"]).astimezone()
        event_date = start_local.date()

        candidates.append(
            {
                "event": event,
                "resolution": resolution,
                "status": calendar_event_status(event, now),
                "event_date": event_date,
                "start_local": start_local,
            }
        )

    # Today's events: NOW, future chronological, past reverse chronological.
    today_items = [item for item in candidates if item["event_date"] == today]
    future_day_items = [item for item in candidates if item["event_date"] > today]

    def today_sort_key(item: dict[str, Any]) -> tuple[int, float]:
        status = item["status"]
        start = item["start_local"].timestamp()

        if status == "now":
            return (0, start)
        if status == "future":
            return (1, start)
        return (2, -start)

    today_items.sort(key=today_sort_key)
    future_day_items.sort(key=lambda item: item["start_local"])

    today_future = [item for item in today_items if item["status"] == "future"]
    next_item = today_future[0] if today_future else None

    def event_alfred_item(
        item: dict[str, Any],
        *,
        future_day: bool = False,
    ) -> dict[str, Any]:
        event = item["event"]
        resolution = item["resolution"]
        status = item["status"]

        event_title = event.get("title") or "(untitled event)"
        event_time = format_event_time(event["start"])

        if future_day:
            day_name = item["start_local"].strftime("%a").upper()
            title = f"{day_name} {event_time} {event_title}"
        elif status == "now":
            title = f"NOW — {event_time} {event_title}"
        elif item is next_item:
            title = f"NEXT — {event_time} {event_title}"
        elif status == "past":
            title = f"EARLIER — {event_time} {event_title}"
        else:
            title = f"LATER — {event_time} {event_title}"

        if resolution["status"] == "resolved":
            ctx = resolution["context"]
            context_id = resolution["context_id"]
            context_name = ctx.get("name", context_id)

            open_space = open_contexts.get(context_id)
            subtitle = context_name

            if resolution["source"] == "rule":
                evidence = ", ".join(resolution.get("evidence", []))
                subtitle = f"{context_name} · {evidence}"
            elif resolution["source"] == "alias":
                subtitle = f"{context_name} · matched '{resolution['alias']}'"

            if calendar_conference_enabled(ctx) and event.get("conference_url"):
                subtitle = f"{subtitle} · JOIN"

            if open_space:
                subtitle = f"OPEN — {open_space} · {subtitle}"

            day_words = [
                item["start_local"].strftime("%A"),
                item["start_local"].strftime("%d %B"),
            ]

            match_terms = [
                event_title,
                context_name,
                context_id,
                event_time,
                status,
                *day_words,
                *calendar_aliases(ctx),
                *event_attendee_emails(event),
                *resolution.get("evidence", []),
            ]

            return {
                "uid": (
                    f"calendar:{event.get('start', '')}:{context_id}:{event_title}"
                ),
                "title": title,
                "subtitle": subtitle,
                "arg": calendar_launch_ref(context_id, event),
                "valid": True,
                "match": " ".join(match_terms).lower(),
            }

        if resolution["status"] == "ambiguous":
            names = [
                match["context"].get("name", match["context_id"])
                for match in resolution["matches"]
            ]
            subtitle = "AMBIGUOUS — " + ", ".join(names)
            match_terms = [event_title, event_time, status, *names]
        else:
            explicit_ref = resolution["explicit_ref"]
            subtitle = f"INVALID ctx: {explicit_ref}"
            match_terms = [event_title, event_time, status, explicit_ref]

        return {
            "uid": f"calendar-problem:{event.get('start', '')}:{event_title}",
            "title": title,
            "subtitle": subtitle,
            "valid": False,
            "match": " ".join(match_terms).lower(),
        }

    items: list[dict[str, Any]] = [event_alfred_item(item) for item in today_items]

    # Append future-day events directly, in chronological order.
    # Prefixing each title with the weekday keeps the view compact while
    # still making the day boundary obvious.
    items.extend(event_alfred_item(item, future_day=True) for item in future_day_items)

    return items


def alfred_now_contexts(query: str = "") -> None:
    """Emit Alfred JSON for today plus the configured short look-ahead horizon."""
    query = query.strip().lower()
    open_contexts = alfred_open_contexts()

    try:
        items = alfred_calendar_items(open_contexts)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "skipknowledge": True,
                    "items": [
                        {
                            "title": "Unable to read calendar contexts",
                            "subtitle": str(exc),
                            "valid": False,
                        }
                    ],
                }
            )
        )
        return

    if query:
        items = [item for item in items if query in item.get("match", "")]

    print(json.dumps({"skipknowledge": True, "items": items}))


def fuzzy_score(query: str, text: str, *, basename_bonus: bool = False) -> int | None:
    """Return a fuzzy-match score, or None when QUERY is not a subsequence.

    Ranking favours exact/prefix/substring matches, then compact subsequences,
    consecutive characters, word/path boundaries and earlier matches.  Notes
    can additionally favour matches concentrated in the basename.
    """
    q = query.strip().casefold()
    t = text.casefold()
    if not q:
        return 0

    # Strong, predictable fast paths.
    if q == t:
        score = 100_000
    elif t.startswith(q):
        score = 90_000 - len(t)
    else:
        pos = t.find(q)
        if pos >= 0:
            score = 80_000 - (pos * 20) - len(t)
        else:
            positions: list[int] = []
            start = 0
            for ch in q:
                pos = t.find(ch, start)
                if pos < 0:
                    return None
                positions.append(pos)
                start = pos + 1

            span = positions[-1] - positions[0] + 1
            gaps = span - len(q)
            consecutive = sum(1 for a, b in zip(positions, positions[1:]) if b == a + 1)
            boundaries = sum(
                1 for pos in positions if pos == 0 or t[pos - 1] in "/-_ ."
            )
            score = (
                50_000
                + consecutive * 250
                + boundaries * 120
                - gaps * 35
                - positions[0] * 8
                - len(t)
            )

    if basename_bonus:
        basename = t.rsplit("/", 1)[-1]
        basename_score = fuzzy_score(q, basename, basename_bonus=False)
        if basename_score is not None:
            score += 5_000

    return score


def alfred_notes(query: str = "") -> None:
    """Emit fuzzy-ranked Alfred Script Filter JSON for notes."""
    config = notes_config()
    root = Path(os.path.expandvars(config["root"])).expanduser()
    extension = config["extension"]
    contexts_root = context_dir().resolve()

    if not root.is_dir():
        raise SystemExit(f"Notes root does not exist or is not a directory: {root}")

    root_resolved = root.resolve()
    exclude_contexts = (
        contexts_root == root_resolved or root_resolved in contexts_root.parents
    )

    default_rel = config["default"].strip().replace("\\", "/")
    if extension and default_rel.casefold().endswith(extension.casefold()):
        default_rel = default_rel[: -len(extension)]

    query = query.strip()
    items = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if extension and not path.name.casefold().endswith(extension.casefold()):
            continue
        resolved = path.resolve()
        if exclude_contexts and (
            resolved == contexts_root or contexts_root in resolved.parents
        ):
            continue

        rel = path.relative_to(root).as_posix()
        if extension and rel.casefold().endswith(extension.casefold()):
            rel = rel[: -len(extension)]

        score = fuzzy_score(query, rel, basename_bonus=True)
        if score is None:
            continue

        is_default = rel.casefold() == default_rel.casefold()
        items.append(
            {
                "title": rel,
                "subtitle": "Open default note" if is_default else "Open note",
                "arg": rel,
                "match": rel,
                "valid": True,
                "_score": score,
                "_is_default": is_default,
            }
        )

    # With no query, preserve the convenient default-note-first behaviour.
    # Once searching, rank purely by fuzzy quality.
    if query:
        items.sort(key=lambda item: (-item["_score"], item["title"].casefold()))
    else:
        items.sort(key=lambda item: (not item["_is_default"], item["title"].casefold()))

    for item in items:
        item.pop("_score", None)
        item.pop("_is_default", None)

    print(json.dumps({"skipknowledge": True, "items": items}))


def alfred_contexts(query: str = "") -> None:
    """Emit fuzzy-ranked Alfred Script Filter JSON for contexts."""
    query = query.strip()
    open_contexts = alfred_open_contexts()

    items = []

    # Normal context registry only; Calendar belongs to ctx alfred-now / cn.
    for ctx in iter_contexts():
        context_id = ctx["_id"]
        name = ctx.get("name", context_id)
        description = ctx.get("description", "")

        searchable = " ".join(
            [
                context_id,
                ctx.get("_file_key", ""),
                name,
                description,
                *ctx.get("_aliases", []),
            ]
        )

        # Score the visible name separately so a good name match beats a
        # coincidental match spread through metadata, while aliases, ids and
        # descriptions remain searchable.
        name_score = fuzzy_score(query, name)
        metadata_score = fuzzy_score(query, searchable)
        scores = [score for score in (name_score, metadata_score) if score is not None]
        if not scores:
            continue
        score = max(scores) + (5_000 if name_score is not None else 0)

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
                "match": searchable.casefold(),
                "valid": True,
                "_score": score,
            }
        )

    if query:
        items.sort(key=lambda item: (-item["_score"], item["title"].casefold()))
    else:
        items.sort(key=lambda item: item["title"].casefold())

    for item in items:
        item.pop("_score", None)

    print(json.dumps({"skipknowledge": True, "items": items}))


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
        searchable = " ".join(
            [
                context_id,
                ctx.get("_file_key", ""),
                name,
                description,
                *ctx.get("_aliases", []),
                space,
            ]
        ).lower()

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


def activate_context(
    context_ref: str,
    *,
    calendar_event: dict[str, Any] | None = None,
) -> None:
    # User-facing commands may supply a name, alias, legacy filename, or id.
    ctx = resolve_context(context_ref)
    context_id = context_id_fn(ctx)
    context_name = ctx.get("name", ctx.get("_file_key", context_id))

    # First repair semantic Space labels following any display change.
    reconcile_space_topology()

    state = reconcile_space_state()

    # Is this context already open? Verify that the recorded Space still
    # contains windows; repair stale state left by an interrupted close.
    for space, active_context in list(state.items()):
        if active_context != context_id:
            continue

        if meaningful_windows_on_space(space):
            focus_space(space)

            conference_url = conference_url_for_context(ctx, calendar_event)
            if conference_url:
                existing_window_ids = {
                    window.get("id")
                    for window in all_windows()
                    if window.get("id") is not None
                }
                chrome = Path(
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                )
                if not chrome.exists():
                    raise SystemExit(
                        "Google Chrome is required for conference URLs but "
                        "was not found in /Applications."
                    )
                subprocess.Popen(
                    [str(chrome), "--new-window", conference_url],
                    start_new_session=True,
                )
                ensure_new_windows_on_space(space, existing_window_ids)

            print(f"Switched to {context_name} on {space}")
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
            if space not in state and not meaningful_windows_on_space(space)
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
        window.get("id") for window in all_windows() if window.get("id") is not None
    }

    # Record allocation before launching, so a partially failed
    # launch doesn't accidentally allow this Space to be reused.
    state[free_space] = context_id
    save_space_state(state)

    try:
        open_context(context_id, calendar_event=calendar_event)
        ensure_new_windows_on_space(free_space, existing_window_ids)
    except Exception:
        state.pop(free_space, None)
        save_space_state(state)
        raise

    print(f"Opened {context_name} on {free_space}")


def activate_calendar_selection(value: str) -> None:
    """Open the exact event selected in Alfred's calendar workflow."""
    parsed = parse_calendar_launch_ref(value)
    if parsed is None:
        raise SystemExit("Invalid calendar selection.")

    expected_context_id, expected_event_key = parsed
    matching_event = next(
        (
            event
            for event in get_calendar_events(ALFRED_CALENDAR_DAYS)
            if calendar_event_key(event) == expected_event_key
        ),
        None,
    )

    if matching_event is None:
        raise SystemExit(
            "That calendar event could no longer be found. "
            "Run cn again and reselect it."
        )

    resolution = resolve_calendar_event(matching_event)
    if resolution.get("status") != "resolved":
        raise SystemExit(
            "That event no longer resolves uniquely to a context. "
            "Run cn again to see the current resolution."
        )

    actual_context_id = resolution["context_id"]
    if actual_context_id != expected_context_id:
        raise SystemExit(
            "That event now resolves to a different context. "
            "Run cn again and reselect it."
        )

    activate_context(actual_context_id, calendar_event=matching_event)


def alfred_switch_contexts(query: str = "") -> None:
    """Emit Alfred Script Filter JSON for currently open contexts only."""
    query = query.strip().lower()
    open_contexts = alfred_open_contexts()

    try:
        current_space = get_current_space()
        current_space_label = current_space.get("label", "")
    except Exception:
        current_space_label = ""

    items = []

    for ctx in iter_contexts():
        context_id = ctx["_id"]
        space = open_contexts.get(context_id)

        if not space:
            continue

        name = ctx.get("name", context_id)
        description = ctx.get("description", "")

        # Switching to the context already occupying the current Space is
        # meaningless, so do not offer it as an Alfred switch target.
        if space == current_space_label:
            continue

        searchable = " ".join([context_id, name, description, space]).lower()

        if query and query not in searchable:
            continue

        subtitle = f"OPEN — {space}"

        if description:
            subtitle += f" · {description}"

        items.append(
            {
                "uid": f"switch:{context_id}",
                "title": name,
                "subtitle": subtitle,
                "arg": context_id,
                "autocomplete": context_id,
                "match": searchable,
                "valid": True,
            }
        )

    print(json.dumps({"skipknowledge": True, "items": items}))


def status_json() -> None:
    """Emit machine-readable state for external ctx clients.

    The reconciled Space assignment remains the source of truth.  Contexts are
    returned in managed-Space order and the context on the currently focused
    Space, if any, is identified separately.
    """
    reconcile_space_topology()
    state = reconcile_space_state()

    try:
        current_space = get_current_space()
        current_space_label = current_space.get("label", "")
    except Exception:
        current_space_label = ""

    contexts_by_id = {context_id_fn(ctx): ctx for ctx in iter_contexts()}
    open_contexts = []
    current_context_id = None

    for space in CTX_SPACES:
        context_id = state.get(space)
        if not context_id:
            continue

        ctx = contexts_by_id.get(context_id)
        is_current = space == current_space_label
        if is_current:
            current_context_id = context_id

        if ctx is None:
            # Preserve a live assignment even if its descriptor is temporarily
            # unavailable, so external clients still see the occupied Space.
            open_contexts.append(
                {
                    "id": context_id,
                    "name": None,
                    "description": None,
                    "space": space,
                    "current": is_current,
                }
            )
            continue

        open_contexts.append(
            {
                "id": context_id,
                "name": ctx.get("name", ctx.get("_file_key", context_id)),
                "description": ctx.get("description", ""),
                "space": space,
                "current": is_current,
            }
        )

    payload = {
        "current_space": current_space_label or None,
        "current_context": current_context_id,
        "contexts": open_contexts,
    }
    print(json.dumps(payload))


def switch_context(context_ref: str) -> None:
    """Switch to an already-open context without opening anything.

    Resolve the context exactly as ``ctx close <ref>`` does, so callers may
    use an immutable id, filename, name, alias, substring, or unique fuzzy
    match.  The Space state itself continues to store immutable context ids.
    """
    reconcile_space_topology()

    ctx = resolve_context(context_ref)
    context_id = context_id_fn(ctx)
    context_name = ctx.get("name", ctx.get("_file_key", context_id))

    state = reconcile_space_state()

    space_label = next(
        (
            space
            for space, active_context in state.items()
            if active_context == context_id
        ),
        None,
    )

    if not space_label:
        raise SystemExit(f"Context '{context_name}' is not currently open.")

    focus_space(space_label)
    print(f"Switched to {context_name} on {space_label}")


def close_context(context_ref: str | None = None) -> None:
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

    if context_ref is None:
        space_label = original_label
        if space_label not in CTX_SPACES:
            raise SystemExit(
                "Current Space is not a managed context Space; "
                "refusing to close its windows."
            )

        context_id = state.get(space_label)
        if not context_id:
            raise SystemExit(f"{space_label} is not currently assigned to a context.")

        try:
            ctx = resolve_context(context_id, fuzzy=False)
            context_name = ctx.get("name", context_id)
        except SystemExit:
            context_name = context_id

        return_label = "main"
    else:
        ctx = resolve_context(context_ref)
        context_id = context_id_fn(ctx)
        context_name = ctx.get("name", ctx.get("_file_key", context_id))

        space_label = next(
            (
                space
                for space, active_context in state.items()
                if active_context == context_id
            ),
            None,
        )
        if not space_label:
            raise SystemExit(f"Context '{context_name}' is not currently open.")

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

    print(f"Released {context_name} from {space_label}")

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


def spaces_command() -> None:
    """Show the live state of managed context Spaces and their windows."""
    reconcile_space_topology()
    state = reconcile_space_state()

    try:
        current_label = get_current_space().get("label", "")
    except Exception:
        current_label = ""

    spaces = yabai("query", "--spaces") or []
    spaces_by_label = {
        space.get("label"): space
        for space in spaces
        if space.get("label") in CTX_SPACES
    }

    contexts_by_id = {context_id_fn(ctx): ctx for ctx in iter_contexts()}

    print(f"{'SPACE':<8} {'INDEX':<6} {'CONTEXT':<28} {'STATE':<8} WINDOWS")

    for label in CTX_SPACES:
        space = spaces_by_label.get(label)
        if space is None:
            print(f"{label:<8} {'-':<6} {'-':<28} {'MISSING':<8} -")
            continue

        context_id = state.get(label)
        if context_id:
            ctx = contexts_by_id.get(context_id)
            context_name = (
                ctx.get("name", ctx.get("_file_key", context_id))
                if ctx is not None
                else context_id
            )
        else:
            context_name = "-"

        raw_windows = windows_on_space(label)
        meaningful = [
            window for window in raw_windows if not window.get("is-sticky", False)
        ]

        if context_id:
            status = "OPEN"
        elif meaningful:
            status = "ORPHAN"
        elif raw_windows:
            status = "STICKY"
        else:
            status = "FREE"

        if label == current_label:
            status += "*"

        print(
            f"{label:<8} {space.get('index', '-')!s:<6} "
            f"{str(context_name):<28.28} {status:<8} {len(meaningful)}"
        )

        for window in raw_windows:
            window_id = window.get("id", "?")
            app = str(window.get("app") or "?")
            title = str(window.get("title") or "")
            flags = []
            if window.get("is-sticky", False):
                flags.append("sticky")
            if window.get("is-visible", False):
                flags.append("visible")
            flag_text = f" [{', '.join(flags)}]" if flags else ""
            print(f"         {window_id}  {app} — {title}{flag_text}")


def clean_spaces() -> None:
    """Interactively clean windows from unassigned managed context Spaces.

    Only Spaces with no logical context assignment are eligible. Sticky
    all-Spaces windows are ignored. The default action is to move orphaned
    windows to ``main`` so recovery is non-destructive.
    """
    reconcile_space_topology()
    state = reconcile_space_state()

    try:
        original_label = get_current_space().get("label", "")
    except Exception:
        original_label = ""

    orphans: list[tuple[str, list[dict[str, Any]]]] = []
    for label in CTX_SPACES:
        if label in state:
            continue
        windows = meaningful_windows_on_space(label)
        if windows:
            orphans.append((label, windows))

    if not orphans:
        print("No orphan windows found on unassigned context Spaces.")
        return

    if not sys.stdin.isatty():
        raise SystemExit("ctx clean requires an interactive terminal.")

    for label, windows in orphans:
        print(f"\n{label}: {len(windows)} orphan window(s)")
        for window in windows:
            print(
                f"  {window.get('id', '?')}  "
                f"{window.get('app', '?')} — {window.get('title', '')}"
            )

        while True:
            choice = (
                input("Move to main [m], close [c], ignore [i], quit [q] [m]: ")
                .strip()
                .lower()
            )
            if choice == "":
                choice = "m"
            if choice in {"m", "c", "i", "q"}:
                break
            print("Please enter m, c, i, or q.")

        if choice == "q":
            break
        if choice == "i":
            continue

        failures: list[dict[str, Any]] = []

        if choice == "m":
            for window in windows:
                window_id = window.get("id")
                if window_id is None:
                    failures.append(window)
                    continue
                try:
                    yabai("window", str(window_id), "--space", "main")
                except RuntimeError:
                    failures.append(window)

            moved = len(windows) - len(failures)
            print(f"Moved {moved} window(s) from {label} to main.")

        elif choice == "c":
            # Accessibility fallback needs the target Space visible. Close
            # ordinary windows first, then leave the Space before closing
            # terminal windows so this command cannot kill its own shell.
            if original_label != label:
                focus_space(label)

            terminal_apps = {"kitty", "Terminal", "iTerm2"}
            ordinary = [
                window for window in windows if window.get("app") not in terminal_apps
            ]
            terminals = [
                window for window in windows if window.get("app") in terminal_apps
            ]

            for window in ordinary:
                if not close_window(window, label):
                    failures.append(window)

            return_label = (
                original_label if original_label and original_label != label else "main"
            )
            try:
                focus_space(return_label)
            except RuntimeError:
                focus_space("main")

            for window in terminals:
                window_id = window.get("id")
                if window_id is None:
                    failures.append(window)
                    continue
                try:
                    yabai("window", str(window_id), "--close")
                except RuntimeError:
                    failures.append(window)

            closed = len(windows) - len(failures)
            print(f"Closed {closed} window(s) from {label}.")

        if failures:
            print("Could not clean some windows:", file=sys.stderr)
            for window in failures:
                print(
                    f"  {window.get('id', '?')}  "
                    f"{window.get('app', '?')} — {window.get('title', '')}",
                    file=sys.stderr,
                )


def close_current_context() -> None:
    """Backward-compatible wrapper for the original no-argument close."""
    close_context()


def complete_contexts() -> None:
    """Emit shell-friendly context completions as NAME\tDESCRIPTION.

    This is a deliberately small private API for shell completion front-ends
    such as Carapace. Canonical human-facing names are emitted; aliases and
    ids remain resolver inputs but are not shown as duplicate completion rows.
    """
    rows: list[tuple[str, str]] = []

    for ctx in iter_contexts():
        name = str(ctx.get("name") or ctx.get("_file_key") or "").strip()
        if not name:
            continue

        description = str(ctx.get("description") or "").strip()

        # Keep the protocol one record per line and two tab-separated fields.
        name = name.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        description = (
            description.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        )
        rows.append((name, description))

    for name, description in sorted(rows, key=lambda row: row[0].casefold()):
        print(f"{name}\t{description}")


def complete_notes() -> None:
    """Emit shell-friendly note completions as ROOT-RELATIVE-NAME\tDESCRIPTION."""
    config = notes_config()
    root = Path(os.path.expandvars(config["root"])).expanduser()
    extension = config["extension"]
    contexts_root = context_dir().resolve()

    if not root.is_dir():
        raise SystemExit(f"Notes root does not exist or is not a directory: {root}")

    root_resolved = root.resolve()
    exclude_contexts = (
        contexts_root == root_resolved or root_resolved in contexts_root.parents
    )

    rows: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if extension and not path.name.casefold().endswith(extension.casefold()):
            continue
        resolved = path.resolve()
        if exclude_contexts and (
            resolved == contexts_root or contexts_root in resolved.parents
        ):
            continue
        rel = path.relative_to(root).as_posix()
        if extension and rel.casefold().endswith(extension.casefold()):
            rel = rel[: -len(extension)]
        rows.append(rel)

    for rel in sorted(rows, key=str.casefold):
        print(f"{rel}\tNote")


def usage() -> None:
    print(
        """Usage:

  ctx list
      List all contexts.

  ctx new
      Create a minimal code context for the current directory. The context is
      named code-<directory-slug>; you are prompted only for its description.

  ctx ensure-ids
      Add an immutable random id to legacy context descriptors that do not
      already have one. Existing names, filenames and content remain usable.

  ctx <name-or-alias>
      Open or switch to a context. Names, aliases, immutable ids and legacy
      filenames are accepted; unambiguous partial/fuzzy names also resolve.

  ctx edit <name-or-alias>
      Open the matching context descriptor in VS Code.

  ctx note [name-or-alias]
      Open the context's notes in the configured VS Code notes profile. With
      no name, use the context on the current Space. Note expressions are
      matched below notes.root from ~/.config/ctx/config.yaml.

  ctx note --print [name-or-alias]
      Print resolved context-note paths without opening them.

  ctx note --root
      Open notes.default from ~/.config/ctx/config.yaml.

  ctx note --open <note>
      Resolve and open one arbitrary note below notes.root. This namespace is
      separate from context names and is suitable for shell/Alfred navigation.

  ctx todo <text> [--url <url>] [--date "YYYY-MM-DD HH:MM"]
      Add a Markdown task at the top of the configured todo inbox. The todo
      file is relative to notes.root unless an absolute path is configured.
      Without --date, the local capture time is used.

  ctx mark <name-or-alias>
      Write a .ctx marker in the current directory containing the context's
      immutable id.

  ctx here
      Walk upward from the current directory to the nearest .ctx marker and
      open/switch to that context.

  ctx unmark
      Remove the .ctx marker from the current directory.

  ctx now
      Open the context associated with the event active now. Explicit ctx:
      notes override automatic matching via calendar.match rules. If the
      context has calendar.conference: true, open that event's meeting link.

  ctx calendar status
      Show the configured calendar provider.

  ctx calendar auth
      Authenticate the configured provider (needed for Google on first use).

  ctx close [name-or-alias]
      With no name, close the context on the current managed Space and return
      to main. With a name, close that context wherever it is open.

  ctx spaces
      Show managed Spaces, their context assignment, occupancy state, and
      yabai-visible windows. ORPHAN means windows exist with no assigned context.

  ctx clean
      Review orphan windows on unassigned managed Spaces. Each Space can be
      moved to main (the default), closed, ignored, or left for later.

  ctx alfred [query]
      Emit Alfred Script Filter JSON for opening/switching contexts.

  ctx alfred-now [query]
      Emit Alfred Script Filter JSON for today and the next few days of
      context-related Calendar events. Explicit ctx: notes override
      automatic calendar.match rules.

  ctx alfred-close [query]
      Emit Alfred Script Filter JSON for currently open contexts.

  ctx status --json
      Emit machine-readable state for currently open contexts.

  ctx switch <context-id>
      Switch to an already-open context without opening anything.

  ctx alfred-switch [query]
      Emit Alfred Script Filter JSON for currently open switch targets.

Examples:

  cd ~/projects/context-manager
  ctx new
  ctx ensure-ids
  ctx COM413
  ctx edit COM413
  ctx note COM413
  ctx note --print COM413
  ctx note --root
  ctx note --open todo
  ctx mark COM413
  ctx here
  ctx close COM413
"""
    )


def main() -> None:
    if len(sys.argv) < 2:
        list_contexts()
        return

    command = sys.argv[1]

    if parse_calendar_launch_ref(command) is not None:
        activate_calendar_selection(command)
        return

    if command in {"-h", "--help", "help"}:
        usage()
        return

    if command == "list":
        list_contexts()
        return

    if command == "now":
        show_now()
        return

    if command == "status":
        if sys.argv[2:] != ["--json"]:
            raise SystemExit("Usage: ctx status --json")
        status_json()
        return

    if command == "calendar":
        calendar_command(sys.argv[2:])
        return

    if command == "new":
        args = sys.argv[2:]
        description = None
        if args:
            if len(args) == 2 and args[0] == "--description":
                description = args[1]
            else:
                raise SystemExit('Usage: ctx new [--description "<description>"]')
        new_code_context(description)
        return

    if command == "ensure-ids":
        ensure_context_ids()
        return

    if command == "_complete":
        if len(sys.argv) != 3 or sys.argv[2] not in {"contexts", "notes"}:
            raise SystemExit("Usage: ctx _complete contexts|notes")
        if sys.argv[2] == "contexts":
            complete_contexts()
        else:
            complete_notes()
        return

    if command == "edit":
        if len(sys.argv) < 3:
            raise SystemExit("Usage: ctx edit <name-or-alias>")
        edit_context(" ".join(sys.argv[2:]))
        return

    if command == "note":
        note_command(sys.argv[2:])
        return

    if command == "todo":
        todo_command(sys.argv[2:])
        return

    if command == "mark":
        if len(sys.argv) < 3:
            raise SystemExit("Usage: ctx mark <name-or-alias>")
        mark_context(" ".join(sys.argv[2:]))
        return

    if command == "unmark":
        unmark_context()
        return

    if command == "here":
        activate_here()
        return

    if command == "alfred":
        args = sys.argv[2:]
        if args and args[0] == "notes":
            alfred_notes(" ".join(args[1:]))
        elif args and args[0] == "contexts":
            alfred_contexts(" ".join(args[1:]))
        else:
            # Backward compatibility for existing Alfred workflows.
            alfred_contexts(" ".join(args))
        return

    if command == "alfred-now":
        query = " ".join(sys.argv[2:])
        alfred_now_contexts(query)
        return

    if command == "alfred-switch":
        query = " ".join(sys.argv[2:])
        alfred_switch_contexts(query)
        return

    if command == "alfred-close":
        query = " ".join(sys.argv[2:])
        alfred_close_contexts(query)
        return

    if command == "close":
        context_ref = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        close_context(context_ref)
        return

    if command == "spaces":
        if len(sys.argv) != 2:
            raise SystemExit("Usage: ctx spaces")
        spaces_command()
        return

    if command == "clean":
        if len(sys.argv) != 2:
            raise SystemExit("Usage: ctx clean")
        clean_spaces()
        return

    if command == "switch":
        if len(sys.argv) < 3:
            raise SystemExit("Usage: ctx switch <name-or-alias>")
        switch_context(" ".join(sys.argv[2:]))
        return

    return activate_context(" ".join(sys.argv[1:]))


if __name__ == "__main__":
    main()
