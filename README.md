# Context Manager

A lightweight macOS context-switching system for opening the
applications, documents, terminals, browser pages, notes, and calendar
resources associated with a piece of work.

The aim is deliberately narrow: **Context Manager is not a
project-management system.** It reduces the friction of switching
between projects while leaving notes and task management in ordinary
Markdown files.

## Overview

Contexts are described by YAML-front-matter Markdown files. A context
can specify resources such as VS Code directories, terminal working
directories, web URLs, ChatGPT project links, documents, notes, and
calendar/conference-call resources.

The surrounding system also includes Calendar integration using
EventKit, an Alfred interface, a macOS menu-bar application
(`ContextBar`), Markdown note lookup, a simple Markdown TODO inbox, and
Gmail-to-TODO capture from Alfred.

## Philosophy

1.  **Contexts describe working environments, not projects.** `ctx` gets
    the material needed for a task onto the screen; it does not attempt
    to manage the task itself.
2.  **Plain files are the source of truth.** Contexts and notes are
    Markdown/YAML rather than application-specific databases.
3.  **Keep capture simple.** Incoming tasks can be placed in a Markdown
    inbox and organised later.
4.  **Prefer small composable tools.** Alfred, Calendar, VS Code, kitty,
    Chrome, Acrobat and `yabai` remain independent applications
    coordinated by `ctx`.

## Context files

Context files live inside the notes tree:

``` text
~/notes/obsidian/contexts/
```

A typical context resembles:

``` yaml
---
name: COM413
description: Principles of Artificial Intelligence

vscode:
  - ~/shared/teaching/Modules/COM413_jb

terminal:
  - ~/shared/teaching/Modules/COM413_jb

urls:
  - label: Module Drive
    url: https://drive.google.com/...

chatgpt:
  - https://chatgpt.com/...
---
```

The contexts directory is part of the Markdown notes repository, but is
excluded from normal note searching.

## Configuration

User configuration lives at:

``` text
~/.config/ctx/config.yaml
```

The configuration defines locations such as the context and notes roots,
together with settings for individual facilities.

For example, TODO capture can be configured with:

``` yaml
todo:
  file: todo.md
  section: Inbox
```

A relative TODO filename is resolved within the configured notes root.

## CLI

The `ctx` command provides the main interface. Current functionality
includes commands for:

``` text
ctx open
ctx close
ctx switch
ctx now
ctx list
ctx mark
ctx edit
ctx new
ctx here
ctx note
ctx todo
```

Use `ctx --help` for the authoritative syntax of the installed version.

### Opening and closing contexts

A context can be opened by name:

``` bash
ctx open COM413
```

Resources associated with it are opened and moved to the appropriate
context Space. Contexts can likewise be closed or switched without
manually finding and rearranging all their windows.

### Creating contexts

`ctx new` creates context metadata for a new working directory, while
`ctx here` supports working from the current directory.

## Notes

`ctx note` provides quick access to Markdown notes in the configured
notes tree. Context definitions themselves live inside that tree but are
excluded from ordinary note search.

## TODO inbox

`ctx todo` provides a deliberately small task-capture mechanism.

The TODO file is ordinary Markdown:

``` markdown
# TODO

## Inbox

- [ ] Reply to Fred about paper ([email](https://mail.google.com/...)) — 2026-09-15 09:18
- [ ] Book room for seminar — 2026-09-15 08:42

## Other sections
```

New tasks are inserted at the **top of the configured Inbox section**,
so the newest captures appear first.

Examples:

``` bash
ctx todo "Book room for seminar"

ctx todo "Reply to Fred about paper" \
  --url "https://mail.google.com/..."

ctx todo "Reply to Fred about paper" \
  --url "https://mail.google.com/..." \
  --date "2026-09-14 16:23"
```

Without an explicit date, `ctx` records the capture time. The command
intentionally does not try to become a full task manager: organisation,
completion and archiving remain conventions within the Markdown notes
system.

## Gmail → TODO

An Alfred workflow recreates the useful part of the former Trello/Gmail
integration.

While an individual Gmail message is open in Chrome:

``` text
Gmail message
    ↓
Alfred hotkey
    ↓
active Chrome tab title + URL
    ↓
ctx todo
    ↓
todo.md / Inbox
```

The workflow reads the active tab of the frontmost Google Chrome window,
verifies that it is an individual Gmail message, cleans the browser
title, passes the subject and Gmail URL to `ctx todo`, and displays an
Alfred notification confirming success or reporting failure.

The resulting TODO entry links directly back to the source email. Gmail
split-pane mode is not currently targeted; normal individual-message
view is used.

## Alfred

Alfred provides shortcuts around the CLI, including context operations
and Gmail-to-TODO capture.

The project-specific Alfred workflows are stored in this repository:

``` text
alfred/
├── context-manager/
└── gmail-todo/
```

Alfred normally stores workflows under:

``` text
~/Library/Application Support/Alfred/Alfred.alfredpreferences/workflows/
```

For this project, the relevant Alfred workflow directories are symlinked
to the repository copies. This makes Git the source of truth without
putting unrelated Alfred preferences under version control.

When setting up another Mac, recreate the symlinks from Alfred's
workflow directory to the corresponding directories under `alfred/`.

## Calendar integration

Calendar integration uses a small Swift/EventKit utility
(`calendar-query`).

Calendar events can associate themselves with a context by placing a
marker such as:

``` text
ctx: COM413
```

in the event Notes. `ctx now` can then identify the relevant current or
upcoming calendar context.

Calendar integration can also locate conference-call links embedded in
events, allowing a context to open the meeting associated with a
calendar event when required.

## macOS Spaces and yabai

Context Manager uses `yabai` to coordinate macOS Spaces. The system
maintains labelled Spaces such as:

``` text
main
ctx-1
ctx-2
ctx-3
```

and adapts the mapping when switching between laptop-only, clamshell and
multi-display configurations.

## ContextBar

`ContextBar` is a small Swift menu-bar application which displays the
currently active context. It polls:

``` bash
ctx status --json
```

and presents the result in the macOS menu bar. The application can be
configured to start automatically at login.

## Applications

The system currently coordinates applications including VS Code, kitty,
Google Chrome, Adobe Acrobat, ChatGPT links, and Apple Calendar.

## Repository layout

The exact layout may evolve, but the project contains components along
these lines:

``` text
context-manager/
├── alfred/
│   ├── context-manager/
│   └── gmail-todo/
├── calendar-query/
├── ContextBar/
├── src/
├── pyproject.toml
└── README.md
```

## Local dependencies

The project is intended for macOS and currently relies on parts of the
following local environment:

-   Python
-   `uv`
-   `yabai`
-   Alfred 5
-   VS Code
-   kitty
-   Google Chrome
-   Apple Calendar / EventKit

Some facilities, particularly window management and browser scripting,
require the corresponding macOS Accessibility or Automation permissions.

## Development and installation

The Python environment is managed with `uv`.

After cloning the repository, configure local paths in:

``` text
~/.config/ctx/config.yaml
```

The configuration file is deliberately kept outside the repository
because it contains machine-specific paths and preferences.

The Alfred workflows, by contrast, are part of the repository. Recreate
their symlinks into Alfred's workflow directory after cloning.

## Status

The system is actively developed for personal use. The emphasis is on
keeping context switching, note access and task capture fast and
predictable rather than expanding `ctx` into a general-purpose
productivity framework.
