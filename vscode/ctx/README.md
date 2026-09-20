# CTX VS Code extension

Minimal VS Code integration for the `ctx` CLI.

## Commands

- **CTX: File TODO…** — files the checkbox task on the current cursor line into a note selected with VS Code Quick Pick.
- **CTX: Sync TODOs** — runs `ctx todo sync`.

The extension deliberately contains no Markdown TODO semantics. Note discovery comes from
`ctx _notes`; filing and synchronisation are performed by the Python CLI.

## Install for development

From `context-manager/vscode/ctx`:

```sh
code --extensionDevelopmentPath="$PWD"
```

For normal local use, open this directory in VS Code and press **F5** to launch an Extension
Development Host. Run **CTX: File TODO…** from the Command Palette.

If `ctx` is not available on VS Code's PATH, set **CTX: Command** (`ctx.command`) to the
absolute path of the `ctx` executable.
