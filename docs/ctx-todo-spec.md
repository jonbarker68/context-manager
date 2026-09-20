# CTX TODO Workflow Specification

**Status:** Initial implementation specification\
**Purpose:** Fix the agreed behaviour of the CTX TODO system before
implementation, so that the CLI and VS Code extension do not drift from
the intended workflow.

## 1. Design goals

The TODO system should remain a plain-Markdown system in which project
notes are the authoritative place for project work, while `todo.md`
provides capture and a persistent high-level view.

The system should:

-   preserve the existing lightweight email-to-TODO capture workflow;
-   allow captured TODOs to be filed into the project note where the
    work belongs;
-   leave a persistent "ghost" in `todo.md` after filing;
-   allow TODOs created directly in project notes to acquire
    corresponding ghosts in `todo.md`;
-   keep ghosts synchronised with their canonical project TODOs without
    reorganising the user's Markdown;
-   preserve links and lightweight metadata such as `[email](...)`,
    `@due:...`, and future tags;
-   use hidden stable IDs only where a synchronised relationship is
    required;
-   keep Markdown and CTX configuration authoritative, with no separate
    task database.

## 2. Roles of the two locations

### `todo.md`

`todo.md` has two related roles.

First, it is the capture point for incoming work, particularly
email-derived TODOs. Existing sections such as `### Today` and
`### This Week` remain ordinary user-maintained Markdown.

For example:

``` markdown
### Today

- [ ] 2026-09-14 ECHI Headscarf Analysis [email](https://mail.google.com/...)
- [ ] 2026-05-28 Retrieve Tascam DR-05's from Wing at end of 2026 @due:2026-06-03
```

These newly captured tasks do **not** need IDs.

Second, `todo.md` contains ghost entries for TODOs whose canonical
version lives in a project note. Ghosts may occur anywhere in `todo.md`.
Their location is user-owned state.

A `## Project TODOs` section is the default landing place for ghosts
created from TODOs first discovered in project notes.

### Project notes

Project notes contain the canonical TODOs for work belonging to that
project or subproject.

Automatic discovery is deliberately restricted to task-list items under
an exact second-level heading:

``` markdown
## TODO

- [ ] Test subtitles on venue equipment
```

This restriction avoids accidentally importing arbitrary or obsolete
checkboxes elsewhere in the notes collection.

## 3. Synchronised task identity

A task enters the synchronisation system when it receives a stable
hidden ID:

``` markdown
- [ ] Test subtitles on venue equipment <!-- todo:a7f3c2 -->
```

The ID represents task identity. The visible text, completion state,
note location, and ghost location may change without changing the ID.

IDs are deliberately location-independent. They must not encode the
project name or file path.

An ordinary task without an ID is not yet participating in
synchronisation.

## 4. Filing an incoming TODO into a project

Suppose `todo.md` contains:

``` markdown
- [ ] 2026-09-14 ECHI Headscarf Analysis [email](https://mail.google.com/...)
```

The user places the cursor on that line in VS Code and invokes **CTX:
File TODO...**.

The UI asks for a destination note using fuzzy matching. Once a
destination is selected:

1.  CTX creates a stable TODO ID.
2.  The full task is moved to the destination note's `## TODO` section.
3.  Existing visible content is preserved, including date, email link,
    `@due:` tag, and any future tags.
4.  The destination task becomes the canonical task.
5.  The original line in `todo.md` is replaced in place by a ghost
    carrying the same ID.

Conceptually:

``` markdown
## TODO

- [ ] 2026-09-14 ECHI Headscarf Analysis [email](https://mail.google.com/...) <!-- todo:a7f3c2 -->
```

and in `todo.md`:

``` markdown
- [ ] → 2026-09-14 ECHI Headscarf Analysis [[echi-note]] <!-- todo:a7f3c2 -->
```

The exact final presentation syntax for the ghost may be refined during
implementation, but its ID and link to the canonical note are required.

The ghost remains where the captured item originally appeared unless the
user subsequently moves it.

## 5. TODOs created directly in project notes

A user may create a TODO naturally while working in a project note:

``` markdown
## TODO

- [ ] Check Blackboard setup for next week's material
```

On `ctx todo sync`, CTX may automatically enrol this task because it is:

-   under `## TODO`;
-   unchecked (`- [ ]`);
-   not already identified.

CTX assigns an ID:

``` markdown
- [ ] Check Blackboard setup for next week's material <!-- todo:81cd42 -->
```

and, if no corresponding ghost exists anywhere in `todo.md`, creates one
under:

``` markdown
## Project TODOs
```

The new ghost is then ordinary user-maintained Markdown and may
subsequently be moved elsewhere in `todo.md`.

## 6. Completed unidentified tasks are not automatically enrolled

An old task such as:

``` markdown
## TODO

- [x] Historical task completed months ago
```

must **not** automatically receive an ID or be introduced into
`todo.md`.

Automatic enrolment applies only to unidentified unchecked `- [ ]` items
under `## TODO`.

Once a task already has an ID, however, both unchecked and checked
states participate in synchronisation.

## 7. Ghost semantics

Every synchronised canonical project TODO should eventually have exactly
one matching ghost in `todo.md`.

A ghost is:

-   a persistent Markdown record, not disposable generated output;
-   identified by the same hidden TODO ID as its canonical task;
-   linked to the project note containing the canonical task;
-   a cached presentation of the canonical task's current text and
    state.

The user owns the ghost's position in `todo.md`.

For example, after completion the user may move a ghost from
`## Project TODOs` or `### Today` to a `## Done` section.
Synchronisation must preserve that placement.

Ghosts must therefore be found by ID across the whole of `todo.md`, not
inferred from their section.

## 8. Synchronisation behaviour

`ctx todo sync` is a conservative reconciliation operation. It must
**not** rebuild `todo.md`.

For each identified canonical task:

``` text
matching ghost exists anywhere in todo.md
    -> update that ghost in place

no matching ghost exists
    -> create a ghost under ## Project TODOs
```

Updating an existing ghost may include:

-   visible task text;
-   checked/unchecked state;
-   link destination if the canonical note has moved or been renamed.

The canonical project TODO is authoritative for synchronised task
content and completion state.

Thus, if the project task changes from:

``` markdown
- [ ] Reply to Fred about display compatibility <!-- todo:7f3a91 -->
```

to:

``` markdown
- [ ] Confirm display resolution and connection with Fred <!-- todo:7f3a91 -->
```

the next sync updates the ghost's visible text without changing its
location in `todo.md`.

If the canonical task becomes:

``` markdown
- [x] Confirm display resolution and connection with Fred <!-- todo:7f3a91 -->
```

the ghost's completion state is updated correspondingly.

## 9. Sync invariants

The initial implementation should enforce or diagnose the following
invariants:

1.  A synchronised task has one stable `todo:<id>`.
2.  A canonical project task may have zero ghosts temporarily, but sync
    creates one if missing.
3.  A synchronised task should have no more than one ghost in `todo.md`.
4.  Ghost location within `todo.md` is never changed by sync.
5.  Existing ghosts are updated in place rather than regenerated.
6.  Project TODO text and completion state are canonical.
7.  Links and lightweight metadata in task text are preserved.
8.  IDs survive task renaming and note movement.
9.  Unidentified completed tasks are ignored by automatic discovery.
10. Arbitrary checkboxes outside `## TODO` are ignored by automatic
    discovery.

Useful diagnostic conditions include duplicate canonical IDs, duplicate
ghosts, and ghosts whose canonical task cannot be found.

## 10. Operations sync must not perform

`ctx todo sync` must not:

-   reorder sections in `todo.md`;
-   move an existing ghost between sections;
-   delete ghosts merely because they have been moved or completed;
-   recreate a ghost just because it is no longer under
    `## Project TODOs`;
-   automatically enrol unidentified checked tasks;
-   interpret arbitrary checkboxes outside `## TODO` as project TODOs;
-   discard email links, `@due:` tags, or unknown future metadata;
-   use a separate task database as the source of truth.

## 11. VS Code extension

A bespoke **CTX** VS Code extension will provide the editor-side UI.

The extension should remain deliberately thin. It may know:

-   the active file;
-   cursor/selection position;
-   the task line under the cursor;
-   user selections from VS Code UI elements such as Quick Pick.

Substantive TODO semantics and filesystem changes should remain in the
Python `ctx` CLI.

The initial important interaction is:

``` text
cursor on TODO
    -> CTX: File TODO...
    -> native VS Code Quick Pick
    -> fuzzy-search destination notes
    -> select destination
    -> ctx performs the filing operation
```

The destination picker should reuse, or faithfully expose, the
fuzzy-matching behaviour already used by CTX/Alfred rather than creating
an unrelated project-selection model.

The extension may also expose operations such as **CTX: Sync TODOs**.

Foam/native Markdown navigation should continue to handle ordinary
navigation through ghost links; the extension need not replace
functionality already provided well by the editor.

## 12. Architectural boundary

The intended architecture is:

``` text
VS Code / Foam
    |
    | thin editor-aware UI
    v
CTX VS Code extension
    |
    | invokes
    v
ctx Python CLI
    |
    | note discovery, TODO semantics, sync, filesystem changes
    v
Markdown notes
```

Markdown files and CTX configuration remain the durable state.

The VS Code extension should not maintain an independent task database.
If the extension were removed, the notes and CLI representation should
remain intelligible and usable.

## 13. Initial implementation scope

The first implementation should concentrate on:

1.  parsing canonical TODOs under `## TODO`;
2.  assigning IDs to newly discovered unchecked project TODOs;
3.  finding task IDs and ghosts reliably;
4.  creating missing ghosts under `## Project TODOs`;
5.  synchronising text, completion state, and destination links in
    place;
6.  filing the TODO under the VS Code cursor into a selected project
    note;
7.  preserving all existing task text/links/tags;
8.  reporting ambiguous or inconsistent ID relationships safely.

Features such as richer tag semantics, due-date processing, automatic
archiving, parent-project aggregation, or more elaborate presentation
should be deferred until the basic lifecycle is reliable.

## 14. Core principle

The implementation should preserve this division of responsibility:

> **Project notes own the work. `todo.md` owns capture, overview, and
> the user's organisation of ghost records. CTX maintains the
> relationships between them without taking ownership of the Markdown
> structure.**
