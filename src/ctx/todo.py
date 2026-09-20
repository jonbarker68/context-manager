import re
import secrets
from datetime import datetime
from pathlib import Path

try:
    from .config import context_dir, notes_config, todo_config, todo_path
except ImportError:
    # Allows direct execution/import during development.
    from config import context_dir, notes_config, todo_config, todo_path


def _todo_timestamp(value: str | None) -> str:
    """Return a normalised todo creation date, defaulting to today."""
    if value is None:
        return datetime.now().astimezone().strftime("%Y-%m-%d")

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(
            "Invalid --date. Use an ISO date/time such as '2026-09-15 09:18'."
        ) from exc

    return parsed.strftime("%Y-%m-%d")


def add_todo(text: str, *, url: str | None = None, date: str | None = None) -> Path:
    """Prepend one task to the configured Markdown todo inbox."""
    path = todo_path()
    if not path.is_file():
        raise SystemExit(f"Todo file does not exist: {path}")

    section = todo_config()["section"].strip()
    content = path.read_text()
    lines = content.splitlines(keepends=True)

    heading_re = re.compile(rf"^#+\s+{re.escape(section)}\s*$", re.IGNORECASE)
    heading_index = next(
        (i for i, line in enumerate(lines) if heading_re.match(line.rstrip("\r\n"))),
        None,
    )
    if heading_index is None:
        raise SystemExit(f"Todo section '{section}' not found in {path}")

    task_text = text.strip()
    if not task_text:
        raise SystemExit("Todo text cannot be empty.")
    if "\n" in task_text or "\r" in task_text:
        raise SystemExit("Todo text must be a single line.")

    if url is not None:
        url = url.strip()
        if not url:
            raise SystemExit("--url cannot be empty.")
        task_text += f" [email]({url})"

    task = f"- [ ] {_todo_timestamp(date)} {task_text}\n"

    # Keep the conventional blank line immediately below the heading, then
    # insert before the existing inbox items so newest captures appear first.
    insert_at = heading_index + 1
    if insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1

    lines.insert(insert_at, task)
    path.write_text("".join(lines))
    return path


def todo_capture_command(args: list[str]) -> None:
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



TODO_ID_RE = re.compile(r"<!--\s*todo:([A-Za-z0-9_-]+)\s*-->")
UNCHECKED_TASK_RE = re.compile(r"^\s*-\s+\[\s\]\s+(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _is_within(path: Path, root: Path) -> bool:
    """Return True if path is root or lies below it."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _scan_note_todos(path: Path) -> list[tuple[int, str, str | None]]:
    """Return unchecked tasks under exact second-level ``## TODO`` sections."""
    results: list[tuple[int, str, str | None]] = []
    in_todo_section = False

    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"Unable to read note {path}: {exc}") from exc

    for line_number, line in enumerate(lines, start=1):
        heading_match = HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            heading = heading_match.group(2).strip()

            if level == 2:
                in_todo_section = heading == "TODO"
            elif level < 2:
                in_todo_section = False

            # Lower-level headings remain inside the current ## TODO section.
            continue

        if not in_todo_section:
            continue

        task_match = UNCHECKED_TASK_RE.match(line)
        if not task_match:
            continue

        task_text = task_match.group(1).strip()
        id_match = TODO_ID_RE.search(task_text)
        todo_id = id_match.group(1) if id_match else None
        if id_match:
            task_text = TODO_ID_RE.sub("", task_text).strip()

        results.append((line_number, task_text, todo_id))

    return results



def _new_todo_id(existing_ids: set[str]) -> str:
    """Return a new 12-hex-character TODO ID not already in use."""
    while True:
        todo_id = secrets.token_hex(6)
        if todo_id not in existing_ids:
            return todo_id


def _all_existing_todo_ids(notes_root: Path, extension: str) -> set[str]:
    """Collect existing TODO IDs across Markdown notes before assigning new ones."""
    existing: set[str] = set()
    pattern = f"*{extension}"
    for path in notes_root.rglob(pattern):
        if not path.is_file():
            continue
        try:
            content = path.read_text()
        except (OSError, UnicodeError) as exc:
            raise SystemExit(f"Unable to read note {path}: {exc}") from exc
        existing.update(TODO_ID_RE.findall(content))
    return existing


def _assign_ids_in_note(path: Path, existing_ids: set[str]) -> int:
    """Assign IDs to unchecked tasks under exact ``## TODO`` sections."""
    try:
        content = path.read_text()
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"Unable to read note {path}: {exc}") from exc

    lines = content.splitlines(keepends=True)
    in_todo_section = False
    changed = 0

    for index, line in enumerate(lines):
        bare_line = line.rstrip("\r\n")
        newline = line[len(bare_line):]

        heading_match = HEADING_RE.match(bare_line)
        if heading_match:
            level = len(heading_match.group(1))
            heading = heading_match.group(2).strip()

            if level == 2:
                in_todo_section = heading == "TODO"
            elif level < 2:
                in_todo_section = False
            continue

        if not in_todo_section:
            continue

        task_match = UNCHECKED_TASK_RE.match(bare_line)
        if not task_match:
            continue

        if TODO_ID_RE.search(bare_line):
            continue

        todo_id = _new_todo_id(existing_ids)
        existing_ids.add(todo_id)
        lines[index] = f"{bare_line} <!-- todo:{todo_id} -->{newline}"
        changed += 1

    if changed:
        path.write_text("".join(lines))

    return changed



def _note_wikilink(path: Path, notes_root: Path, extension: str) -> str:
    """Return a Foam-style wikilink target for a note path."""
    relative = path.relative_to(notes_root).as_posix()
    if extension and relative.endswith(extension):
        relative = relative[:-len(extension)]
    return relative


def _canonical_project_todos(
    notes_root: Path,
    extension: str,
    configured_todo: Path,
    configured_contexts: Path,
) -> list[tuple[Path, int, str, str, bool]]:
    """Return identified canonical TODOs: path, line, text, id, checked."""
    results: list[tuple[Path, int, str, str, bool]] = []
    pattern = f"*{extension}"
    task_re = re.compile(r"^\s*-\s+\[([ xX])\]\s+(.*)$")

    for path in sorted(notes_root.rglob(pattern)):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved == configured_todo:
            continue
        if _is_within(resolved, configured_contexts):
            continue

        try:
            lines = path.read_text().splitlines()
        except (OSError, UnicodeError) as exc:
            raise SystemExit(f"Unable to read note {path}: {exc}") from exc

        in_todo_section = False
        for line_number, line in enumerate(lines, start=1):
            heading_match = HEADING_RE.match(line)
            if heading_match:
                level = len(heading_match.group(1))
                heading = heading_match.group(2).strip()
                if level == 2:
                    in_todo_section = heading == "TODO"
                elif level < 2:
                    in_todo_section = False
                continue

            if not in_todo_section:
                continue

            task_match = task_re.match(line)
            if not task_match:
                continue

            body = task_match.group(2).strip()
            id_match = TODO_ID_RE.search(body)
            if not id_match:
                continue

            todo_id = id_match.group(1)
            text = TODO_ID_RE.sub("", body).strip()
            checked = task_match.group(1).lower() == "x"
            results.append((path, line_number, text, todo_id, checked))

    return results


def _sync_project_ghosts(
    canonical: list[tuple[Path, int, str, str, bool]],
    notes_root: Path,
    extension: str,
) -> tuple[int, int]:
    """Create missing ghosts and update existing ghosts in place."""
    config = todo_config()
    project_section = config["project_section"]
    path = todo_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        content = path.read_text()
    else:
        content = ""

    lines = content.splitlines(keepends=True)

    # Index all ghost IDs anywhere in todo.md. Duplicate IDs are unsafe.
    ghost_locations: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        match = TODO_ID_RE.search(line)
        if match:
            ghost_locations.setdefault(match.group(1), []).append(index)

    duplicates = {todo_id: locs for todo_id, locs in ghost_locations.items() if len(locs) > 1}
    if duplicates:
        details = ", ".join(sorted(duplicates))
        raise SystemExit(f"Duplicate TODO ghosts in {path}: {details}")

    canonical_by_id: dict[str, tuple[Path, int, str, str, bool]] = {}
    for item in canonical:
        todo_id = item[3]
        if todo_id in canonical_by_id:
            first = canonical_by_id[todo_id]
            raise SystemExit(
                "Duplicate canonical TODO ID "
                f"{todo_id}: {first[0]}:{first[1]} and {item[0]}:{item[1]}"
            )
        canonical_by_id[todo_id] = item

    updated = 0
    missing: list[str] = []

    for todo_id, (note_path, _line_number, text, _id, checked) in canonical_by_id.items():
        checkbox = "x" if checked else " "
        link = _note_wikilink(note_path, notes_root, extension)
        ghost_line = f"- [{checkbox}] {text} → [[{link}]] <!-- todo:{todo_id} -->"

        locations = ghost_locations.get(todo_id)
        if locations:
            index = locations[0]
            old = lines[index]
            newline = "\n"
            if old.endswith("\r\n"):
                newline = "\r\n"
            elif old.endswith("\n"):
                newline = "\n"
            elif old.endswith("\r"):
                newline = "\r"
            else:
                newline = ""
            replacement = ghost_line + newline
            if old != replacement:
                lines[index] = replacement
                updated += 1
        else:
            missing.append(ghost_line)

    if missing:
        current = "".join(lines)

        # Append the configured project section only if it does not exist.
        section_re = re.compile(
            rf"^###[ \t]+{re.escape(project_section)}[ \t]*$",
            re.MULTILINE,
        )
        if not section_re.search(current):
            if current and not current.endswith("\n"):
                current += "\n"
            if current and not current.endswith("\n\n"):
                current += "\n"
            current += f"### {project_section}\n\n"
            lines = current.splitlines(keepends=True)

        # Insert new ghosts at the end of the configured section, before the
        # next level-1/2 heading. Existing ghosts elsewhere are never moved.
        start = None
        for index, line in enumerate(lines):
            if line.rstrip("\r\n") == f"### {project_section}":
                start = index + 1
                break

        if start is None:
            raise SystemExit(f"Unable to locate project TODO section: {project_section}")

        insert_at = len(lines)
        for index in range(start, len(lines)):
            heading_match = HEADING_RE.match(lines[index].rstrip("\r\n"))
            if heading_match and len(heading_match.group(1)) <= 3:
                insert_at = index
                break

        block = [ghost + "\n" for ghost in missing]
        # Keep a blank line around the inserted task block where practical.
        if insert_at > 0 and lines[insert_at - 1].strip():
            block.insert(0, "\n")
        if insert_at < len(lines) and lines[insert_at].strip():
            block.append("\n")
        lines[insert_at:insert_at] = block

    if updated or missing or not path.exists():
        path.write_text("".join(lines))

    return len(missing), updated


def _resolve_note_argument(value: str, notes_root: Path, extension: str) -> Path:
    """Resolve an absolute or notes-root-relative Markdown note argument."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = notes_root / path

    if not path.suffix and extension:
        path = Path(str(path) + extension)

    return path.resolve()


def _append_canonical_todo(
    destination: Path,
    task_line: str,
    todo_id: str,
) -> None:
    """Append a task beneath exact ``## TODO``, creating the section if needed."""
    try:
        content = destination.read_text() if destination.exists() else ""
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"Unable to read destination note {destination}: {exc}") from exc

    lines = content.splitlines(keepends=True)
    section_start = None
    section_end = len(lines)

    for index, line in enumerate(lines):
        heading_match = HEADING_RE.match(line.rstrip("\r\n"))
        if not heading_match:
            continue
        level = len(heading_match.group(1))
        heading = heading_match.group(2).strip()
        if level == 2 and heading == "TODO":
            section_start = index + 1
            for later in range(section_start, len(lines)):
                next_heading = HEADING_RE.match(lines[later].rstrip("\r\n"))
                if next_heading and len(next_heading.group(1)) <= 2:
                    section_end = later
                    break
            break

    canonical_line = f"{task_line} <!-- todo:{todo_id} -->\n"

    if section_start is None:
        current = "".join(lines)
        if current and not current.endswith("\n"):
            current += "\n"
        if current and not current.endswith("\n\n"):
            current += "\n"
        current += "## TODO\n\n"
        current += canonical_line
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(current)
        return

    block = [canonical_line]
    if section_end > section_start and lines[section_end - 1].strip():
        block.insert(0, "\n")
    if section_end < len(lines) and lines[section_end].strip():
        block.append("\n")
    lines[section_end:section_end] = block
    destination.write_text("".join(lines))


def todo_file_command(args: list[str]) -> None:
    """File one Markdown task into a project note and leave a ghost in place."""
    source_arg = None
    destination_arg = None
    line_number = None

    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--source":
            index += 1
            if index >= len(args):
                raise SystemExit("ctx todo file: --source requires a value")
            source_arg = args[index]
        elif arg.startswith("--source="):
            source_arg = arg.split("=", 1)[1]
        elif arg == "--destination":
            index += 1
            if index >= len(args):
                raise SystemExit("ctx todo file: --destination requires a value")
            destination_arg = args[index]
        elif arg.startswith("--destination="):
            destination_arg = arg.split("=", 1)[1]
        elif arg == "--line":
            index += 1
            if index >= len(args):
                raise SystemExit("ctx todo file: --line requires a value")
            try:
                line_number = int(args[index])
            except ValueError:
                raise SystemExit("ctx todo file: --line must be an integer")
        elif arg.startswith("--line="):
            try:
                line_number = int(arg.split("=", 1)[1])
            except ValueError:
                raise SystemExit("ctx todo file: --line must be an integer")
        else:
            raise SystemExit(f"ctx todo file: unknown argument: {arg}")
        index += 1

    if source_arg is None or destination_arg is None or line_number is None:
        raise SystemExit(
            "Usage: ctx todo file --source <note> --line <number> "
            "--destination <note>"
        )
    if line_number < 1:
        raise SystemExit("ctx todo file: --line must be at least 1")

    notes_root = Path(notes_config()["root"]).expanduser().resolve()
    extension = notes_config()["extension"] or ".md"
    source = _resolve_note_argument(source_arg, notes_root, extension)
    destination = _resolve_note_argument(destination_arg, notes_root, extension)

    if not _is_within(source, notes_root):
        raise SystemExit(f"Source is outside notes root: {source}")
    if not _is_within(destination, notes_root):
        raise SystemExit(f"Destination is outside notes root: {destination}")
    if source == destination:
        raise SystemExit("ctx todo file: source and destination must differ")
    if not source.is_file():
        raise SystemExit(f"Source note does not exist: {source}")
    if not destination.is_file():
        raise SystemExit(f"Destination note does not exist: {destination}")

    try:
        source_content = source.read_text()
        destination_content = destination.read_text()
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"Unable to read TODO files: {exc}") from exc

    source_lines = source_content.splitlines(keepends=True)
    if line_number > len(source_lines):
        raise SystemExit(
            f"ctx todo file: line {line_number} is beyond end of {source}"
        )

    original = source_lines[line_number - 1]
    bare = original.rstrip("\r\n")
    newline = original[len(bare):]

    task_match = re.match(r"^(\s*)-\s+\[([ xX])\]\s+(.*)$", bare)
    if not task_match:
        raise SystemExit(
            f"ctx todo file: {source}:{line_number} is not a Markdown checkbox task"
        )
    if TODO_ID_RE.search(bare):
        raise SystemExit(
            f"ctx todo file: {source}:{line_number} already has a TODO ID"
        )

    indent, checked_mark, task_text = task_match.groups()

    # Validate ID uniqueness before either file is written.
    existing_ids = _all_existing_todo_ids(notes_root, extension)
    todo_id = _new_todo_id(existing_ids)

    # Preserve the complete visible task content, but canonicalise it as a
    # top-level project task. Dates, links and tags remain untouched.
    canonical_task = f"- [{checked_mark}] {task_text}"

    # Prepare the ghost before writing anything.
    link = _note_wikilink(destination, notes_root, extension)
    ghost = (
        f"{indent}- [{checked_mark}] {task_text} "
        f"→ [[{link}]] <!-- todo:{todo_id} -->"
    )

    # Write destination first. If this succeeds, replace the source line.
    # The operation is deliberately conservative: all validation occurs first.
    _append_canonical_todo(destination, canonical_task, todo_id)
    source_lines[line_number - 1] = ghost + newline
    try:
        source.write_text("".join(source_lines))
    except OSError as exc:
        raise SystemExit(
            "Filed canonical TODO but failed to replace source with ghost; "
            f"run ctx todo sync after repairing the source file: {exc}"
        ) from exc

    relative = destination.relative_to(notes_root)
    print(f"Filed TODO to {relative} <!-- todo:{todo_id} -->")

def todo_sync_command(args: list[str]) -> None:
    """Synchronise canonical project TODOs with their ghosts in todo.md."""
    if args:
        raise SystemExit("Usage: ctx todo sync")

    notes_root = Path(notes_config()["root"]).expanduser()
    if not notes_root.is_dir():
        raise SystemExit(f"Notes root does not exist: {notes_root}")

    configured_todo = todo_path().resolve()
    configured_contexts = context_dir().resolve()
    extension = notes_config()["extension"] or ".md"
    pattern = f"*{extension}"

    existing_ids = _all_existing_todo_ids(notes_root, extension)
    assigned = 0

    # Enrol unidentified unchecked project TODOs.
    for path in sorted(notes_root.rglob(pattern)):
        if not path.is_file():
            continue

        resolved = path.resolve()
        if resolved == configured_todo:
            continue
        if _is_within(resolved, configured_contexts):
            continue

        count = _assign_ids_in_note(path, existing_ids)
        if count:
            relative = path.relative_to(notes_root)
            print(f"{relative}: assigned {count} TODO ID{'s' if count != 1 else ''}")
            assigned += count

    canonical = _canonical_project_todos(
        notes_root,
        extension,
        configured_todo,
        configured_contexts,
    )
    created, updated = _sync_project_ghosts(canonical, notes_root, extension)

    if assigned:
        print(f"Assigned {assigned} TODO ID{'s' if assigned != 1 else ''}.")
    if created:
        print(f"Created {created} project TODO ghost{'s' if created != 1 else ''}.")
    if updated:
        print(f"Updated {updated} project TODO ghost{'s' if updated != 1 else ''}.")
    if not assigned and not created and not updated:
        print("TODOs already in sync.")


def todo_scan_command(args: list[str]) -> None:
    """Report project TODOs without modifying any Markdown."""
    if args:
        raise SystemExit("Usage: ctx todo scan")

    notes_root = Path(notes_config()["root"]).expanduser()
    notes_root = Path(str(notes_root)).expanduser()
    if not notes_root.is_dir():
        raise SystemExit(f"Notes root does not exist: {notes_root}")

    configured_todo = todo_path().resolve()
    configured_contexts = context_dir().resolve()
    extension = notes_config()["extension"] or ".md"
    pattern = f"*{extension}"

    found = 0
    for path in sorted(notes_root.rglob(pattern)):
        if not path.is_file():
            continue

        resolved = path.resolve()
        if resolved == configured_todo:
            continue
        if _is_within(resolved, configured_contexts):
            continue

        tasks = _scan_note_todos(path)
        if not tasks:
            continue

        relative = path.relative_to(notes_root)
        for line_number, task_text, todo_id in tasks:
            status = f"[{todo_id}]" if todo_id else "[untracked]"
            print(f"{relative}:{line_number}")
            print(f"    {status} {task_text}")
            found += 1

    if found == 0:
        print("No unchecked project TODOs found.")

def todo_command(args: list[str]) -> None:
    """Dispatch the todo command namespace.

    The historical ``ctx todo <text>`` form remains the default capture
    behaviour. Named subcommands are reserved for the structured TODO
    workflow being added incrementally.
    """
    if not args:
        todo_capture_command(args)
        return

    subcommand = args[0]

    if subcommand == "add":
        todo_capture_command(args[1:])
        return

    if subcommand == "scan":
        todo_scan_command(args[1:])
        return

    if subcommand == "sync":
        todo_sync_command(args[1:])
        return

    if subcommand == "file":
        todo_file_command(args[1:])
        return

    # Backward compatibility: anything else is an inbox capture.
    todo_capture_command(args)
