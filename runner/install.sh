#!/bin/zsh
# Install or remove the macOS login agents.
#   runner/install.sh runner      idle-time runner, checks every 30 minutes
#   runner/install.sh dashboard   local dashboard server at http://localhost:8790
#   runner/install.sh remove runner|dashboard
set -e
here="$(cd "$(dirname "$0")/.." && pwd)"
uv="$(command -v uv)"
agents="$HOME/Library/LaunchAgents"
name="$1"
if [ "$name" = "remove" ]; then
  launchctl bootout "gui/$(id -u)/com.coldcase.$2" 2>/dev/null || true
  rm -f "$agents/com.coldcase.$2.plist"
  echo "removed com.coldcase.$2"
  exit 0
fi
[ -f "$here/runner/$name.plist.template" ] || { echo "usage: install.sh runner|dashboard|remove <name>"; exit 1; }
mkdir -p "$agents" "$here/cases"
sed -e "s|__PROJECT__|$here|g" -e "s|__UV__|$uv|g" "$here/runner/$name.plist.template" > "$agents/com.coldcase.$name.plist"
launchctl bootout "gui/$(id -u)/com.coldcase.$name" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$agents/com.coldcase.$name.plist"
echo "installed com.coldcase.$name"
