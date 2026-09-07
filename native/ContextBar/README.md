# ContextBar

Thin macOS menu-bar front end for the `ctx` CLI.

## Important

Do not test this with `swift run`.

A command-line Swift executable is not the same thing as a packaged macOS UI application.
Build the `.app` bundle first:

```bash
./scripts/build-app.sh
open ContextBar.app
```

`Info.plist` sets `LSUIElement=true`, so ContextBar runs as a menu-bar accessory without
a Dock icon or ordinary application menu.

ContextBar delegates all state and actions to:

- `ctx status --json`
- `ctx switch <id>`
- `ctx close <id>`


## Login Items

ContextBar explicitly supplies a normal command-line PATH to `ctx` subprocesses.
macOS Login Items do not inherit the PATH from an interactive zsh session, so this
ensures tools such as Homebrew-installed `yabai` can be found after login.
