import os
from pathlib import Path
from typing import Any

import yaml

CONTEXT_CONFIG_FILE = Path("~/.config/ctx/config.yaml").expanduser()

DEFAULT_CONTEXTS_CONFIG = {
    "root": "~/shared/notes/contexts",
}

DEFAULT_NOTES_CONFIG = {
    "root": "~/shared/notes",
    "extension": ".md",
    "vscode_profile": "Foam Notes",
    "default": "index.md",
}

DEFAULT_TODO_CONFIG = {
    "file": "todo.md",
    "capture_section": "Inbox",
    "project_section": "Project TODOs",
}


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
    """Return TODO configuration with backward-compatible section names."""
    config = read_context_config().get("todo", {}) or {}
    merged = dict(DEFAULT_TODO_CONFIG)
    merged.update(config)

    # Legacy `section` meant the capture/inbox section.
    if "capture_section" not in config and "section" in config:
        merged["capture_section"] = config["section"]

    return merged


def todo_path() -> Path:
    """Return the configured todo file, relative to notes.root unless absolute."""
    configured = Path(os.path.expandvars(todo_config()["file"])).expanduser()
    if configured.is_absolute():
        return configured

    notes_root = Path(os.path.expandvars(notes_config()["root"])).expanduser()
    return notes_root / configured
