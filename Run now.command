#!/bin/zsh
# Double-click to start one runner batch now, even while the Mac is in use.
# It stops at the limits in runner.toml; press Ctrl-C to stop it sooner.
cd "$(dirname "$0")" || exit 1
uv run python -I -m coldcase.runner run --now
echo
echo "Batch finished. You can close this window."
