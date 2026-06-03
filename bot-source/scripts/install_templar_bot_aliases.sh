#!/usr/bin/env sh
# Install bot-level shortcut commands into /usr/local/bin by default.
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="${1:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)}"
TARGET_DIR="${TEMPLAR_BOT_ALIAS_DIR:-/usr/local/bin}"
DISPATCHER="$TARGET_DIR/templar_bot_alias"

COMMANDS="bot_commands templar_bot_commands add_banner set_banner list_banners reset_banner"

install -d -m 0755 "$TARGET_DIR"
install -m 0755 "$REPO_DIR/scripts/templar_bot_alias" "$DISPATCHER"
for name in $COMMANDS; do
  ln -sf "$DISPATCHER" "$TARGET_DIR/$name"
done

echo "Installed Templar bot shortcuts to $TARGET_DIR"
echo "Run: bot_commands"
