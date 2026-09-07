#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

swift build -c release

APP="$ROOT/ContextBar.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"

rm -rf "$APP"
mkdir -p "$MACOS"

cp "$ROOT/.build/release/ContextBar" "$MACOS/ContextBar"
cp "$ROOT/Info.plist" "$CONTENTS/Info.plist"

echo "Built:"
echo "  $APP"
echo
echo "Run with:"
echo "  open \"$APP\""
